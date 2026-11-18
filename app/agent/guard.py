"""Guard：动作提议进入执行器前的五道校验

注意：这里是**预筛**，不是最终闸门。`execute_with_check()` 内部的安全规则
校验必须保留，防止 TOCTOU 与其他调用方绕过。
"""

from __future__ import annotations

import logging

from app.agent.proposals import ALLOWED_ACTIONS, ActionProposal
from app.core.config import settings
from app.tools.rules import check_safety_rules

logger = logging.getLogger(__name__)

_DEVICE_TYPES = ("aerator", "pump", "feeder")


def _device_of(action: str) -> str:
    for device in _DEVICE_TYPES:
        if device in action:
            return device
    return ""


def _more_conservative(old: ActionProposal, new: ActionProposal) -> ActionProposal:
    """同一设备出现多个动作时保留更保守的一个（异常场景下 start 优先于 stop）"""
    if old.action.startswith("start_") and new.action.startswith("stop_"):
        return old
    if old.action.startswith("stop_") and new.action.startswith("start_"):
        return new
    return old


def validate(proposals: list[ActionProposal], context: dict) -> list[ActionProposal]:
    """返回通过全部校验、可送执行器的动作提议

    ① Pydantic 枚举/参数校验（在 ActionProposal 构造时已完成）
    ② 动作白名单（防御性复查）
    ③ 同设备动作冲突检测
    ④ check_safety_rules 预筛：硬性拦截丢弃，需人工确认的放行给执行器
    ⑤ 动作数量上限
    """
    sensors = context.get("all_sensors") or {}
    pond_id = context.get("pond_id", "")
    device_id = context.get("device_id", "")

    kept: list[ActionProposal] = []
    by_device: dict[str, ActionProposal] = {}

    for proposal in proposals:
        if proposal.action not in ALLOWED_ACTIONS or proposal.action == "no_action":
            continue

        device = _device_of(proposal.action)
        if device and device in by_device:
            previous = by_device[device]
            winner = _more_conservative(previous, proposal)
            if winner is previous:
                continue
            kept = [p for p in kept if p is not previous]
            by_device[device] = winner
            chosen = winner
        else:
            if device:
                by_device[device] = proposal
            chosen = proposal

        safety = check_safety_rules(pond_id, device_id, chosen.action, sensors)
        if not safety.allowed and not safety.requires_approval:
            logger.info("[Agent][Guard] 安全规则预筛拦截: %s | %s",
                        chosen.action, safety.reason)
            if device and by_device.get(device) is chosen:
                del by_device[device]
            continue

        kept.append(chosen)
        if len(kept) >= settings.AGENT_MAX_ACTIONS:
            break

    return kept
