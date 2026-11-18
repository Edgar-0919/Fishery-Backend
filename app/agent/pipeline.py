"""Agent 决策链路：上下文构建 → ReAct 循环 → 告警决策记录

与执行层的边界：
- 上下文查询复用 services.agent_pipeline 的私有查询
- 告警记录写入复用 _upsert_alert_duration
- 设备动作只经 tools.executor.execute_with_check 唯一出口
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.agent.loop import run_agent
from app.agent.proposals import AgentRun
from app.core.db import execute_sql, safe_identifier

logger = logging.getLogger(__name__)


def _build_context(alert) -> dict:
    """构建全局上下文（所有传感器 + 设备状态 + 近 10 分钟历史）"""
    from app.services.agent_pipeline import _get_all_sensors_latest, _get_device_status

    now = datetime.now()
    start = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    end = now.strftime("%Y-%m-%d %H:%M:%S")

    all_sensors = _get_all_sensors_latest(alert.pond_id, alert.device_id)
    device_status = _get_device_status(alert.pond_id)

    from app.core.config import settings

    sql = f"""
        SELECT ts, {safe_identifier(alert.sensor_type)} FROM fishery.sensor_data
        WHERE pond_id = %s AND device_id = %s
          AND ts >= %s AND ts <= %s
        ORDER BY ts DESC
        LIMIT %s
    """
    result = execute_sql(
        sql,
        (alert.pond_id, alert.device_id, start, end,
         settings.AGENT_CONTEXT_HISTORY_LIMIT),
    )
    history = []
    if result.get("code") == 0 and result.get("data"):
        history = [{"ts": row[0], "value": row[1]} for row in result["data"]]

    return {
        "alert_type": alert.alert_type,
        "sensor_type": alert.sensor_type,
        "current_value": alert.value,
        "threshold": alert.threshold,
        "severity": alert.severity,
        "trend": alert.trend,
        "pond_id": alert.pond_id,
        "device_id": alert.device_id,
        "message": alert.message,
        "history_10min": history,
        "all_sensors": all_sensors,
        "device_status": device_status,
    }


def _minimal_context(alert) -> dict:
    """上下文查询失败时的最小可用上下文，保证 Agent 仍能走规则兜底"""
    return {
        "alert_type": alert.alert_type,
        "sensor_type": alert.sensor_type,
        "current_value": alert.value,
        "threshold": alert.threshold,
        "severity": alert.severity,
        "trend": alert.trend,
        "pond_id": alert.pond_id,
        "device_id": alert.device_id,
        "message": alert.message,
        "history_10min": [],
        "all_sensors": {},
        "device_status": {},
    }


def _describe(run: AgentRun) -> str:
    if run.outcome == "fallback":
        return f"Agent 降级到规则兜底（{run.fallback_reason}）"
    reasons = [p.get("reason", "") for p in run.proposals if p.get("reason")]
    return "；".join(reasons)[:500] or "Agent 已完成决策"


def agent_loop_pipeline(alert) -> AgentRun:
    """完整决策链路，返回审计记录"""
    try:
        context = _build_context(alert)
    except Exception as exc:
        logger.warning("[Agent] 上下文构建失败，使用最小上下文: %s", exc)
        context = _minimal_context(alert)

    run = run_agent(alert, context)

    action = "no_action"
    if run.final_actions:
        action = run.final_actions[0].get("action") or "no_action"

    try:
        from app.services.agent_pipeline import _upsert_alert_duration

        _upsert_alert_duration(alert, _describe(run), action, datetime.now())
    except Exception as exc:
        logger.warning("[Agent] 告警决策记录写入失败: %s", exc)

    return run
