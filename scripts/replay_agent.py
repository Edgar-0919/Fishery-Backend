"""离线回放：用历史告警重跑 Agent 决策，只提议不执行

用途：改 prompt、换模型、调预算后，用真实告警分布验证行为变化，
不产生任何设备动作。生产链路永远走 ``run_agent(execute=True)``，
本脚本是 dry-run 的唯一入口。

用法：
    python scripts/replay_agent.py --limit 20
    python scripts/replay_agent.py --alert-type dissolved_oxygen_low --since-hours 72
    python scripts/replay_agent.py --limit 50 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.loop import run_agent
from app.agent.pipeline import _build_context, _minimal_context
from app.core.db import execute_mysql
from app.models.domain import AlertEvent

_SUFFIXES = ("_trend_rising", "_trend_falling", "_low", "_high")


def _sensor_of(alert_type: str) -> str:
    for suffix in _SUFFIXES:
        if alert_type.endswith(suffix):
            return alert_type[: -len(suffix)]
    return alert_type


def _trend_of(alert_type: str) -> str:
    if alert_type.endswith("_trend_rising"):
        return "rising"
    if alert_type.endswith("_trend_falling"):
        return "falling"
    return "stable"


def _load_alerts(limit: int, since_hours: int, alert_type: str | None) -> list[dict]:
    sql = """
        SELECT pond_id, device_id, alert_type, alert_value, threshold_value, message
        FROM alerts
        WHERE ts >= DATE_SUB(NOW(), INTERVAL %s HOUR)
    """
    params: list = [since_hours]
    if alert_type:
        sql += " AND alert_type = %s"
        params.append(alert_type)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    return execute_mysql(sql, tuple(params)) or []


def _to_event(row: dict) -> AlertEvent:
    alert_type = str(row.get("alert_type") or "")
    return AlertEvent(
        pond_id=str(row.get("pond_id") or ""),
        device_id=str(row.get("device_id") or ""),
        sensor_type=_sensor_of(alert_type),
        value=float(row.get("alert_value") or 0),
        threshold=float(row.get("threshold_value") or 0),
        severity="critical",
        trend=_trend_of(alert_type),
        alert_type=alert_type,
        message=str(row.get("message") or ""),
    )


def _replay(alert: AlertEvent) -> dict:
    try:
        context = _build_context(alert)
    except Exception as exc:
        print(f"  上下文构建失败，使用最小上下文: {exc}")
        context = _minimal_context(alert)

    run = run_agent(alert, context, execute=False)
    return {
        "alert_type": alert.alert_type,
        "pond_id": alert.pond_id,
        "device_id": alert.device_id,
        "value": alert.value,
        "threshold": alert.threshold,
        "outcome": run.outcome,
        "steps": run.steps,
        "latency_ms": run.latency_ms,
        "fallback_reason": run.fallback_reason,
        "proposals": run.proposals,
        "final_actions": run.final_actions,
        "rag_queries": run.rag_queries,
        "error": run.error,
    }


def _render(item: dict, index: int, total: int) -> None:
    print(f"[{index}/{total}] {item['alert_type']} "
          f"pond={item['pond_id']} dev={item['device_id']} "
          f"value={item['value']} threshold={item['threshold']}")
    print(f"  outcome={item['outcome']} steps={item['steps']} "
          f"latency={item['latency_ms']}ms rag_queries={len(item['rag_queries'])}")

    if item["proposals"]:
        for proposal in item["proposals"]:
            params = json.dumps(proposal.get("params", {}), ensure_ascii=False)
            print(f"  提议: {proposal.get('action')} {params} "
                  f"— {proposal.get('reason', '')}")
    else:
        print(f"  提议: 无（{item['fallback_reason'] or '模型未给出合法动作'}）")

    for action in item["final_actions"]:
        params = json.dumps(action.get("params", {}), ensure_ascii=False)
        print(f"  若执行: {action.get('action')} {params} "
              f"source={action.get('source')} status={action.get('status')}")

    if item["error"]:
        print(f"  error: {item['error']}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="离线回放 Agent 决策（只提议不执行）")
    parser.add_argument("--limit", type=int, default=20, help="回放条数，默认 20")
    parser.add_argument("--since-hours", type=int, default=24, help="取最近多少小时的告警")
    parser.add_argument("--alert-type", default=None, help="只回放指定告警类型")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于脚本比对")
    args = parser.parse_args()

    rows = _load_alerts(args.limit, args.since_hours, args.alert_type)
    if not rows:
        print("没有可回放的告警：检查数据库连接、时间窗口或 alert_type 过滤条件")
        return 1

    results = [_replay(_to_event(row)) for row in rows]

    if args.json:
        print(json.dumps(results, ensure_ascii=False, default=str, indent=2))
    else:
        for index, item in enumerate(results, start=1):
            _render(item, index, len(results))

        kept = sum(1 for item in results if item["final_actions"])
        fallback = sum(1 for item in results if item["fallback_reason"])
        print(f"汇总: {len(results)} 条 | 有可执行提议 {kept} | 降级 {fallback}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
