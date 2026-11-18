"""规则兜底决策

阈值判断已由 detector 完成，本模块只根据「指标 + 越界方向」查
``settings.FALLBACK_ACTIONS`` 决定处置动作，不重新判定数值，
避免出现「告警阈值 8.5、紧急阈值 9.5、兜底判断 9.0」这类多套不一致。

调用方：``app/agent/loop.py`` 的 ``_fallback()``，在 LLM 异常、超时、
超预算或无合法提议时降级使用；兜底动作同样走
``app.tools.executor.execute_with_check()`` 唯一出口。
"""

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


def _resolve_direction(alert_type: str, current_value, threshold) -> str:
    """从告警类型推断越界方向

    Args:
        alert_type: 告警类型，如 dissolved_oxygen_low
        current_value: 当前值
        threshold: 触发阈值

    Returns:
        str: "high" 或 "low"
    """
    if "_trend_rising" in alert_type:
        return "high"
    if "_trend_falling" in alert_type:
        return "low"
    if "_high" in alert_type:
        return "high"
    if "_low" in alert_type:
        return "low"

    try:
        return "high" if float(current_value) > float(threshold) else "low"
    except (TypeError, ValueError):
        return "high"


def _rule_based_decision(alert_context: dict) -> dict:
    """规则引擎兜底决策：LLM 不可用时按配置表生成决策

    Args:
        alert_context: 告警上下文，包含 alert_type / sensor_type / current_value / threshold

    Returns:
        dict: 与 Agent 决策同结构的字典（analysis / decision / follow_up）
    """
    alert_type = alert_context.get("alert_type", "")
    sensor_type = alert_context.get("sensor_type", "")
    current_value = alert_context.get("current_value", 0)
    threshold = alert_context.get("threshold", 0)

    direction = _resolve_direction(alert_type, current_value, threshold)
    rule = settings.FALLBACK_ACTIONS.get(sensor_type, {}).get(direction)

    if not rule:
        logger.warning(
            "规则兜底无匹配配置，转人工: sensor=%s type=%s direction=%s",
            sensor_type, alert_type, direction,
        )
        return {
            "analysis": f"{alert_type}：{current_value}，LLM 不可用且无匹配兜底规则",
            "decision": {
                "action": "alert_human",
                "params": {
                    "priority": "medium",
                    "reason": f"告警类型: {alert_type}, 值: {current_value}, 阈值: {threshold}",
                },
                "reason": "LLM 调用失败，触发人工审核",
            },
            "follow_up": "请人工检查水质状况并决定是否需要干预",
        }

    logger.info(
        "规则兜底命中: sensor=%s type=%s direction=%s action=%s",
        sensor_type, alert_type, direction, rule["action"],
    )

    return {
        "analysis": f"{alert_type}：当前值 {current_value}，阈值 {threshold}，LLM 不可用，按规则兜底处置",
        "decision": {
            "action": rule["action"],
            "params": rule.get("params", {}),
            "reason": rule.get("reason", ""),
        },
        "follow_up": rule.get("follow_up", ""),
    }


# Agent 链路降级时调用的公开入口
fallback_decision = _rule_based_decision
