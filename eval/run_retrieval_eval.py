#!/usr/bin/env python
"""RAG 检索层评测 —— 纯向量 / 纯 BM25 / RRF 混合 三路消融

目的：为「混合检索优于任何单一检索」这一设计假设提供可复现的量化证据，
而不是依赖注释里的经验判断。

用法：
    cd Backend
    python -m eval.run_retrieval_eval                      # 默认按线上配置
    python -m eval.run_retrieval_eval --top-k 5
    python -m eval.run_retrieval_eval --score-threshold 0   # 关闭相似度过滤
    python -m eval.run_retrieval_eval --json                # 机器可读，供 CI 使用
    python -m eval.run_retrieval_eval --gate-hit-rate 0.75  # 低于阈值退出码 1

评测口径默认与线上一致（settings.RAG_TOP_K / RAG_SCORE_THRESHOLD），
否则测出来的数字不代表真实行为。

前置条件：Chroma 索引已构建
    python -m app.rag.build_index

评测集格式（eval/dataset/retrieval_cases.jsonl，每行一条）：
    id                唯一编号
    query             用户问法（原始口语）
    relevant_sources  相关文档（相对 knowledge/ 的路径）
    keywords          相关段落中必须出现的词（任一命中即算 chunk 级命中）
    bucket            exact（精确术语）| paraphrase（同义改写）| multi_hop（跨文档）
    min_sources       需要覆盖的最少文档数，默认 1（multi_hop 通常为 2）

指标定义：
    Hit@k      top-k 内是否达成「覆盖 min_sources 篇相关文档」
    Recall@k   top-k 内覆盖的相关文档数 / 标注的相关文档数
    MRR@k      首个达成覆盖的排名的倒数均值（未达成记 0）

关于 multi_hop：该桶要求覆盖 2 篇不同文档，Hit@k 天然偏低，
应以 Recall@k 作为主观察指标，它衡量跨文档问题的信息覆盖度。

公平性说明：三路策略均只在 top-k 结果上参与评分。混合路径内部虽各自取
top-5 再融合，但对外同样截断到 top-k，与单路策略口径一致。相似度阈值仅
作用于向量命中（与 retrieve() 内部实现一致），BM25 分数无统一下界故不设阈值。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_DATASET_PATH = Path(__file__).resolve().parent / "dataset" / "retrieval_cases.jsonl"

BUCKETS = ("exact", "paraphrase", "multi_hop")
STRATEGY_LABELS = {
    "vector_only": "vector only (bge + Chroma cosine)",
    "bm25_only": "bm25 only (jieba tokens)",
    "rrf_hybrid": "RRF hybrid (k=60)",
}


# --------------------------------------------------------------------- 数据结构

@dataclass
class Case:
    id: str
    query: str
    relevant_sources: list[str]
    keywords: list[str] = field(default_factory=list)
    bucket: str = "exact"
    min_sources: int = 1


@dataclass
class Metric:
    """一组用例的聚合指标"""

    n: int = 0
    hit: int = 0
    mrr_sum: float = 0.0
    recall_sum: float = 0.0

    def add(self, hit: bool, recall: float, depth: int | None) -> None:
        self.n += 1
        self.hit += int(hit)
        self.recall_sum += recall
        self.mrr_sum += (1.0 / depth) if depth else 0.0

    @property
    def hit_rate(self) -> float:
        return self.hit / self.n if self.n else 0.0

    @property
    def recall(self) -> float:
        return self.recall_sum / self.n if self.n else 0.0

    @property
    def mrr(self) -> float:
        return self.mrr_sum / self.n if self.n else 0.0

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "hit_rate": round(self.hit_rate, 4),
            "recall": round(self.recall, 4),
            "mrr": round(self.mrr, 4),
        }


# --------------------------------------------------------------------- 命中判定

def _norm_source(value: str) -> str:
    """归一化 source：统一分隔符，并去掉可能存在的 knowledge/ 前缀"""
    text = (value or "").replace("\\", "/").strip()
    marker = "knowledge/"
    idx = text.find(marker)
    if idx >= 0:
        text = text[idx + len(marker):]
    return text.lstrip("./")


def _result_source(item: dict) -> str:
    """兼容三种检索器返回结构：扁平 source 字段 / metadata.source"""
    meta = item.get("metadata") or {}
    return _norm_source(item.get("source") or meta.get("source", ""))


def _hit_sources(item: dict, case: Case) -> set[str]:
    """返回该结果命中的标注文档集合（文档路径匹配 + 关键词命中）"""
    source = _result_source(item)
    matched = {
        want for want in case.relevant_sources
        if source == _norm_source(want) or source.endswith(_norm_source(want))
    }
    if not matched:
        return set()

    if case.keywords:
        content = item.get("content") or ""
        if not any(kw in content for kw in case.keywords):
            return set()
    return matched


def _score_case(results: list[dict], case: Case, top_k: int) -> tuple[int | None, float]:
    """返回（达成覆盖的最小深度, top-k 内召回率）"""
    need = max(1, case.min_sources)
    covered: set[str] = set()
    depth: int | None = None

    for rank, item in enumerate(results[:top_k], start=1):
        covered |= _hit_sources(item, case)
        if depth is None and len(covered) >= need:
            depth = rank

    total = len(set(case.relevant_sources)) or 1
    return depth, len(covered) / total


# --------------------------------------------------------------------- 策略与执行

def _make_strategies(score_threshold: float):
    """构建三路检索策略

    单路策略直接调用底层检索函数（``_keyword_search`` 虽为私有，但评测必须
    拿到未融合的单路结果，这是使用它的唯一场景），混合策略走统一入口。
    """
    from app.rag.embeddings import embed_text
    from app.rag.retriever import _keyword_search, retrieve
    from app.rag.vector_store import search_by_embedding

    def vector_only(query: str, top_k: int) -> list[dict]:
        hits = search_by_embedding(embed_text(query), top_k=top_k)
        # 与 retrieve() 内部一致：阈值仅作用于向量命中
        return [h for h in hits if (h.get("score") or 0.0) >= score_threshold]

    def bm25_only(query: str, top_k: int) -> list[dict]:
        return _keyword_search(query, top_k=top_k)

    def rrf_hybrid(query: str, top_k: int) -> list[dict]:
        return retrieve(query, top_k=top_k, score_threshold=score_threshold)

    return {
        "vector_only": vector_only,
        "bm25_only": bm25_only,
        "rrf_hybrid": rrf_hybrid,
    }


def _run_strategy(retrieve_fn, cases: list[Case], top_k: int) -> dict:
    """执行一路策略，返回 {bucket: Metric} + 整体 Metric"""
    buckets: dict[str, Metric] = {name: Metric() for name in BUCKETS}
    overall = Metric()

    for case in cases:
        results = retrieve_fn(case.query, top_k)
        depth, recall = _score_case(results, case, top_k)
        hit = depth is not None

        overall.add(hit, recall, depth)
        buckets.setdefault(case.bucket, Metric()).add(hit, recall, depth)

    return {"buckets": buckets, "overall": overall}


# --------------------------------------------------------------------- 报告

def _fmt_row(name: str, bucket: str, metric: Metric) -> str:
    return (
        f"{name:<13} {bucket:<12} "
        f"{metric.hit_rate:>7.3f} {metric.recall:>10.3f} "
        f"{metric.mrr:>7.3f} {metric.n:>5}"
    )


def _print_table(results: dict[str, dict], top_k: int) -> None:
    header = (
        f"{'strategy':<13} {'bucket':<12} "
        f"{'Hit@' + str(top_k):>7} {'Recall@' + str(top_k):>10} "
        f"{'MRR@' + str(top_k):>7} {'n':>5}"
    )
    print(header)
    print("-" * len(header))

    for name, payload in results.items():
        for bucket in BUCKETS:
            if bucket in payload["buckets"]:
                print(_fmt_row(name, bucket, payload["buckets"][bucket]))
        print(_fmt_row(name, "ALL", payload["overall"]))
        print("-" * len(header))


def _delta_pt(base: Metric, target: Metric) -> float:
    """百分点差值"""
    return round((target.hit_rate - base.hit_rate) * 100, 1)


def _print_delta(results: dict[str, dict], top_k: int) -> dict:
    """报告混合路径相对更强单路的提升，以及分桶差异"""
    hybrid = results["rrf_hybrid"]
    baselines = {name: results[name] for name in ("vector_only", "bm25_only")}
    stronger = max(baselines, key=lambda name: baselines[name]["overall"].hit_rate)

    summary = {
        "stronger_baseline": stronger,
        "overall_pt": _delta_pt(baselines[stronger]["overall"], hybrid["overall"]),
        "by_bucket": {},
    }
    for bucket in BUCKETS:
        base = baselines[stronger]["buckets"].get(bucket)
        target = hybrid["buckets"].get(bucket)
        if base and target:
            summary["by_bucket"][bucket] = _delta_pt(base, target)

    print()
    print(f"hybrid vs stronger baseline ({stronger}):")
    print(f"  Hit@{top_k} ALL        {summary['overall_pt']:+.1f}pt")
    for bucket, delta in summary["by_bucket"].items():
        print(f"  Hit@{top_k} {bucket:<12} {delta:+.1f}pt")

    # 诊断：每个桶的最优策略，用于判断两路检索的互补性是否真实存在
    print()
    print("per-bucket best strategy (by Hit@%d):" % top_k)
    for bucket in BUCKETS:
        ranked = sorted(
            (
                (name, payload["buckets"][bucket])
                for name, payload in results.items()
                if bucket in payload["buckets"]
            ),
            key=lambda item: item[1].hit_rate,
            reverse=True,
        )
        if not ranked:
            continue
        best_name, best_metric = ranked[0]
        detail = " / ".join(f"{n} {m.hit_rate:.3f}" for n, m in ranked)
        print(f"  {bucket:<12} {best_name:<13} {detail}")

    if summary["overall_pt"] < 0:
        print()
        print("  [!] hybrid is BELOW the best single path - check RRF k or candidate depth")
    return summary


# --------------------------------------------------------------------- 入口

def _load_cases(path: Path) -> list[Case]:
    if not path.is_file():
        raise SystemExit(f"[error] dataset not found: {path}")

    cases: list[Case] = []
    for lineno, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[error] line {lineno} is not valid JSON: {exc}") from exc
        cases.append(
            Case(
                id=raw["id"],
                query=raw["query"],
                relevant_sources=raw["relevant_sources"],
                keywords=raw.get("keywords") or [],
                bucket=raw.get("bucket", "exact"),
                min_sources=int(raw.get("min_sources", 1)),
            )
        )
    if not cases:
        raise SystemExit(f"[error] dataset is empty: {path}")
    return cases


def _ensure_index() -> int:
    """确认向量索引已构建，返回 chunk 数"""
    from app.rag.vector_store import get_collection_count

    try:
        count = get_collection_count()
    except Exception as exc:  # noqa: BLE001 索引损坏或依赖缺失时给出可操作提示
        raise SystemExit(
            f"[error] cannot read vector index: {exc}\n"
            "run: python -m app.rag.build_index"
        ) from exc

    if count == 0:
        raise SystemExit(
            "[error] Chroma index is empty.\n"
            "build it first: python -m app.rag.build_index"
        )
    return count


def _production_defaults() -> tuple[int, float]:
    """读取线上检索口径，缺失时回落到硬编码默认值"""
    try:
        from app.core.config import settings

        return int(settings.RAG_TOP_K), float(settings.RAG_SCORE_THRESHOLD)
    except Exception:  # noqa: BLE001 评测不应因为 .env 缺失而失败
        return 3, 0.5


def main() -> int:
    default_top_k, default_threshold = _production_defaults()

    parser = argparse.ArgumentParser(description="RAG retrieval ablation evaluation")
    parser.add_argument("--top-k", type=int, default=default_top_k, help="截断深度")
    parser.add_argument(
        "--score-threshold",
        type=float,
        default=default_threshold,
        help="向量相似度阈值（仅作用于向量命中）",
    )
    parser.add_argument("--dataset", type=Path, default=_DATASET_PATH, help="评测集路径")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--gate-hit-rate",
        type=float,
        default=None,
        help="整体 Hit@k 门禁阈值，低于该值返回退出码 1",
    )
    args = parser.parse_args()

    if args.top_k < 1:
        raise SystemExit("[error] --top-k must be >= 1")

    cases = _load_cases(args.dataset)
    chunk_count = _ensure_index()

    bucket_counts = {name: sum(1 for c in cases if c.bucket == name) for name in BUCKETS}
    if not args.json:
        print(
            f"dataset : {len(cases)} cases "
            f"({' / '.join(f'{k} {v}' for k, v in bucket_counts.items() if v)})"
        )
        print(f"index   : {chunk_count} chunks")
        print(f"config  : top_k={args.top_k}, score_threshold={args.score_threshold}")
        print("loading embedding model, please wait ...\n")

    strategies = _make_strategies(args.score_threshold)
    results: dict[str, dict] = {}
    for name, fn in strategies.items():
        results[name] = _run_strategy(fn, cases, args.top_k)
        if not args.json:
            print(f"  [done] {STRATEGY_LABELS[name]}")

    overall = results["rrf_hybrid"]["overall"]

    if args.json:
        payload = {
            "top_k": args.top_k,
            "score_threshold": args.score_threshold,
            "dataset_size": len(cases),
            "bucket_counts": bucket_counts,
            "chunks": chunk_count,
            "strategies": {
                name: {
                    "overall": data["overall"].as_dict(),
                    "buckets": {
                        bucket: metric.as_dict()
                        for bucket, metric in data["buckets"].items()
                    },
                }
                for name, data in results.items()
            },
            "hybrid_vs_vector_pt": _delta_pt(
                results["vector_only"]["overall"], overall
            ),
            "hybrid_vs_bm25_pt": _delta_pt(
                results["bm25_only"]["overall"], overall
            ),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print()
        _print_table(results, args.top_k)
        _print_delta(results, args.top_k)

    if args.gate_hit_rate is not None and overall.hit_rate < args.gate_hit_rate:
        print(
            f"\n[gate failed] hybrid Hit@{args.top_k} = {overall.hit_rate:.3f} "
            f"< threshold {args.gate_hit_rate:.3f}"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
