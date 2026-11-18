"""混合检索器 —— BM25 关键词 + 向量语义 + RRF 融合

检索策略：
1. 向量检索：ChromaDB Cosine 相似度，top_k=5
2. 关键词检索：jieba 分词 + BM25 稀疏检索，top_k=5
3. RRF 融合：Reciprocal Rank Fusion 合并两路结果，去重后取 top_k=3

RRF 公式：score(d) = Σ 1/(k + rank_i(d))，k=60（经典参数）

为什么用混合检索：
- 纯向量检索对"烂鳃病""氨氮中毒"等精确术语匹配不如关键词
- 纯关键词检索对同义表达（"缺氧"→"溶解氧偏低"）召回不足
- 混合检索两者互补，召回率提升 30-50%
"""

from __future__ import annotations

import logging
import os
from rank_bm25 import BM25Okapi
import jieba

from app.rag.embeddings import embed_text
from app.rag.vector_store import search_by_embedding, get_all_documents

logger = logging.getLogger(__name__)

# RRF 融合参数
_RRF_K = 60

# 全局单例：BM25 索引（懒加载，首次检索时构建）
_bm25_index: BM25Okapi | None = None
_bm25_docs: list[dict] | None = None


def _tokenize(text: str) -> list[str]:
    """jieba 分词（BM25 需要分词后的 token 列表）"""
    return list(jieba.cut(text))


def _build_bm25_index(force_rebuild: bool = False) -> None:
    """构建/重建 BM25 关键词索引（从 ChromaDB 中取出所有文档块）

    Args:
        force_rebuild: 是否强制重建索引
    """
    global _bm25_index, _bm25_docs
    if _bm25_index is not None and not force_rebuild:
        return

    logger.info("[RAG] 正在构建 BM25 关键词索引 ...")
    docs = get_all_documents()
    _bm25_docs = docs

    if not docs:
        logger.warning("[RAG] 向量存储为空，BM25 索引为空")
        _bm25_index = None
        return

    # 分词后构建 BM25 索引
    tokenized = [_tokenize(doc["content"]) for doc in docs]
    _bm25_index = BM25Okapi(tokenized)
    logger.info(f"[RAG] BM25 索引构建完成，共 {len(docs)} 个文档块")


def _keyword_search(query: str, top_k: int = 5) -> list[dict]:
    """BM25 关键词检索

    Args:
        query: 查询文本
        top_k: 返回结果数量

    Returns:
        list[dict]: [{id, content, metadata, score}, ...]
    """
    _build_bm25_index()
    if _bm25_index is None or _bm25_docs is None:
        return []

    tokenized_query = _tokenize(query)
    scores = _bm25_index.get_scores(tokenized_query)

    # 按分数降序排列，取 top_k
    indexed_scores = list(enumerate(scores))
    indexed_scores.sort(key=lambda x: x[1], reverse=True)
    top_indices = indexed_scores[:top_k]

    results = []
    for idx, score in top_indices:
        if score <= 0:
            continue
        doc = _bm25_docs[idx]
        results.append({
            "id": doc["id"],
            "content": doc["content"],
            "metadata": doc.get("metadata", {}),
            "score": round(float(score), 4)
        })
    return results


def _rrf_fusion(
    vector_results: list[dict],
    keyword_results: list[dict],
    top_k: int = 3
) -> list[dict]:
    """RRF（Reciprocal Rank Fusion）融合两路检索结果

    算法：
    - 对每个结果，计算 RRF 分数 = 1/(k + rank)
    - 向量路径 rank 和关键词路径 rank 分别独立计算
    - 同一文档块在两路中都有时，RRF 分数相加
    - 按 RRF 分数降序排列，取 top_k

    Args:
        vector_results: 向量检索结果
        keyword_results: 关键词检索结果
        top_k: 最终返回数量

    Returns:
        list[dict]: 融合排序后的结果
    """
    rrf_scores = {}
    doc_map = {}

    # 向量检索结果 → RRF 分数，并记录向量余弦相似度（0-1）
    for rank, doc in enumerate(vector_results):
        rrf_score = 1.0 / (_RRF_K + rank + 1)
        doc_id = doc["id"]
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0) + rrf_score
        if doc_id not in doc_map:
            doc_map[doc_id] = doc.copy()
        doc_map[doc_id]["vector_score"] = doc.get("score", 0)

    # 关键词检索结果 → RRF 分数，并记录关键词匹配分数
    for rank, doc in enumerate(keyword_results):
        rrf_score = 1.0 / (_RRF_K + rank + 1)
        doc_id = doc["id"]
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0) + rrf_score
        if doc_id not in doc_map:
            doc_map[doc_id] = doc.copy()
        doc_map[doc_id]["keyword_score"] = doc.get("score", 0)

    # 按 RRF 分数降序排列
    sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    top_ids = sorted_ids[:top_k]

    results = []
    for doc_id in top_ids:
        doc = doc_map[doc_id].copy()
        doc["rrf_score"] = round(rrf_scores[doc_id], 4)
        results.append(doc)

    return results


def retrieve(
    query: str,
    top_k: int = 3,
    source_filter: str | None = None,
    score_threshold: float = 0.0
) -> list[dict]:
    """混合检索统一入口：向量 + BM25 → RRF 融合

    使用方式：
        from app.rag import retrieve
        results = retrieve("溶解氧偏低怎么处理", top_k=3)

    Args:
        query: 查询文本
        top_k: 返回结果数量
        source_filter: 按 source 过滤
        score_threshold: 最低相似度阈值，低于此值的结果不返回

    Returns:
        list[dict]: 检索结果 [{content, source, score, chunk_id, category}, ...]
    """
    if not query or not query.strip():
        return []

    # 1. 向量检索
    query_embedding = embed_text(query)
    vector_results = search_by_embedding(query_embedding, top_k=5, source_filter=source_filter)

    # 2. 关键词检索
    keyword_results = _keyword_search(query, top_k=5)

    # 3. RRF 融合
    fused = _rrf_fusion(vector_results, keyword_results, top_k=top_k)

    # 4. 格式化输出 + 质量过滤
    #    注意：RRF 分数量级约为 0.01（1/61），与余弦相似度（0-1）不在同一量级，
    #    因此阈值过滤必须基于向量余弦相似度（vector_score），而非 RRF 分数，
    #    否则 score_threshold=0.5 会把所有结果误过滤掉，导致前端「参考来源」恒为空。
    formatted = []
    for doc in fused:
        metadata = doc.get("metadata", {})
        vector_score = doc.get("vector_score")
        # 仅对向量命中结果做阈值过滤；纯关键词命中的结果不参与向量阈值判断
        if vector_score is not None and vector_score < score_threshold:
            continue
        # 返回前端的 score 优先取向量相似度（0-1），便于展示为百分比
        display_score = vector_score if vector_score is not None else doc.get("rrf_score", doc.get("score", 0))
        formatted.append({
            "content": doc.get("content", ""),
            "source": metadata.get("source", "unknown"),
            "score": round(display_score, 4),
            "chunk_id": doc.get("id", ""),
            "category": metadata.get("category", ""),
            "title": metadata.get("title", "")
        })

    if formatted:
        logger.info(f"[RAG] 检索完成: query='{query[:50]}...', 返回 {len(formatted)} 条, top_score={formatted[0]['score']:.4f}")
    else:
        logger.info(f"[RAG] 检索无结果: query='{query[:50]}...'")

    return formatted


class HybridRetriever:
    """混合检索器类（封装状态，便于依赖注入）"""

    def __init__(self):
        self._initialized = False

    def initialize(self):
        """初始化检索器：预加载 BM25 索引"""
        if not self._initialized:
            _build_bm25_index(force_rebuild=True)
            self._initialized = True
            logger.info("[RAG] HybridRetriever 初始化完成")

    def retrieve(self, query: str, top_k: int = 3, score_threshold: float = 0.0) -> list[dict]:
        """检索接口（与模块级 retrieve 函数一致）"""
        return retrieve(query, top_k=top_k, score_threshold=score_threshold)

    def rebuild_index(self):
        """重建 BM25 索引（知识库更新后调用）"""
        _build_bm25_index(force_rebuild=True)
        logger.info("[RAG] BM25 索引已重建")