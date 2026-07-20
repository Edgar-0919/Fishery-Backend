"""规则引擎：三层防御体系的核心规则定义

三层防御架构：
1. 紧急规则（Emergency Rules）：最高优先级，绕过 LLM 直接执行，用于极端危险场景
2. 安全规则（Safety Rules）：执行前校验，拦截危险指令，保护设备和鱼类安全
3. 效果跟踪规则（Effect Tracking Rules）：执行后验证，检查指令是否产生预期效果

配置参数（来自 settings）：
- COOLDOWN_MINUTES: 同一设备冷却期（默认 5 分钟）
- EFFECT_CHECK_MINUTES: 效果检查间隔（默认 15 分钟）
- HIGH_RISK_ACTIONS: 需要人工确认的高风险动作列表
"""

from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from app.core.config import settings


@dataclass
class RuleResult:
    """规则匹配结果"""
    matched: bool
    action: str = ""
    params: dict = None
    reason: str = ""
    bypass_llm: bool = False  # 是否绕过 LLM 直接执行


@dataclass
class SafetyCheckResult:
    """安全校验结果"""
    allowed: bool
    blocked_by: str = ""  # 被哪个规则拦截
    reason: str = ""
    requires_approval: bool = False  # 是否需要人工确认


def check_emergency_rules(pond_id: str, device_id: str, sensor_data: Dict[str, float]) -> Optional[RuleResult]:
    """检查紧急规则，匹配时直接执行（绕过 LLM）

    紧急规则优先级高于 LLM 决策，用于极端危险场景，避免 LLM 延迟导致错过响应窗口。

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        sensor_data: 所有传感器的当前值字典

    Returns:
        Optional[RuleResult]: 匹配的规则结果，无匹配时返回 None

    紧急规则列表：
    1. DO < 1.0mg/L → 立即开增氧机（最高优先级，极度危险）
    2. 温度 > 35℃ → 立即开增氧机降温
    3. pH < 6.0 或 > 9.5 → 立即报警（极端 pH 会直接导致鱼类死亡）
    4. 氨氮 > 1.0mg/L → 立即开水泵换水
    5. 水位 < 0.5m → 立即报警（水位过低）
    """
    do = sensor_data.get("dissolved_oxygen", 100)
    temp = sensor_data.get("temperature", 0)
    ph = sensor_data.get("ph_value", 7.0)
    ammonia = sensor_data.get("ammonia_nitrogen", 0)
    water_level = sensor_data.get("water_level", 10)

    if do < 1.0:
        return RuleResult(
            matched=True,
            action="start_aerator",
            params={"power": "high", "duration": 60},
            reason=f"溶解氧极度危险({do:.2f}mg/L < 1.0mg/L)，立即启动增氧机最高功率",
            bypass_llm=True
        )

    if temp > 35.0:
        return RuleResult(
            matched=True,
            action="start_aerator",
            params={"power": "high", "duration": 30},
            reason=f"水温过高({temp:.1f}℃ > 35℃)，立即启动增氧机降温",
            bypass_llm=True
        )

    if ph < 6.0 or ph > 9.5:
        return RuleResult(
            matched=True,
            action="alert_human",
            params={"priority": "critical"},
            reason=f"pH 值严重异常({ph:.2f})，需人工紧急处理",
            bypass_llm=True
        )

    if ammonia > 1.0:
        return RuleResult(
            matched=True,
            action="start_pump",
            params={"duration": 30},
            reason=f"氨氮严重超标({ammonia:.2f}mg/L > 1.0mg/L)，立即启动水泵换水",
            bypass_llm=True
        )

    if water_level < 0.5:
        return RuleResult(
            matched=True,
            action="alert_human",
            params={"priority": "critical"},
            reason=f"水位过低({water_level:.2f}m < 0.5m)，需人工紧急处理",
            bypass_llm=True
        )

    return None


def check_safety_rules(pond_id: str, device_id: str, action: str, sensor_data: Dict[str, float]) -> SafetyCheckResult:
    """检查安全规则，拦截危险指令

    在执行 LLM 决策前进行安全校验，确保不会执行危害鱼类安全的操作。

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        action: 待执行的动作（如 start_aerator, stop_aerator）
        sensor_data: 所有传感器的当前值字典

    Returns:
        SafetyCheckResult: 安全校验结果，包含是否允许执行、拦截原因等

    安全规则列表：
    1. DO < 3mg/L 时禁止关闭增氧机（最关键的安全规则）
    2. DO < 3mg/L 时禁止关闭水泵（换水也能增氧）
    3. pH < 6.5 或 > 8.5 时禁止关闭水泵（需要持续换水调节）
    4. 温度 > 30℃ 时禁止关闭增氧机（高温需持续增氧）
    5. 高风险动作（stop_aerator, stop_pump）默认需要人工确认
    """
    do = sensor_data.get("dissolved_oxygen", 100)
    ph = sensor_data.get("ph_value", 7.0)
    temp = sensor_data.get("temperature", 0)

    if action == "stop_aerator":
        if do < 3.0:
            return SafetyCheckResult(
                allowed=False,
                blocked_by="DO Safety Rule",
                reason=f"溶解氧({do:.2f}mg/L)低于安全阈值(3mg/L)，禁止关闭增氧机"
            )
        if temp > 30.0:
            return SafetyCheckResult(
                allowed=False,
                blocked_by="Temperature Safety Rule",
                reason=f"水温过高({temp:.1f}℃)，禁止关闭增氧机"
            )
        if action in settings.HIGH_RISK_ACTIONS:
            return SafetyCheckResult(
                allowed=False,
                requires_approval=True,
                reason="关闭增氧机属于高风险操作，需人工确认"
            )

    if action == "stop_pump":
        if do < 3.0:
            return SafetyCheckResult(
                allowed=False,
                blocked_by="DO Safety Rule",
                reason=f"溶解氧({do:.2f}mg/L)低于安全阈值(3mg/L)，禁止关闭水泵"
            )
        if ph < 6.5 or ph > 8.5:
            return SafetyCheckResult(
                allowed=False,
                blocked_by="pH Safety Rule",
                reason=f"pH 值异常({ph:.2f})，禁止关闭水泵（需持续换水调节）"
            )
        if action in settings.HIGH_RISK_ACTIONS:
            return SafetyCheckResult(
                allowed=False,
                requires_approval=True,
                reason="关闭水泵属于高风险操作，需人工确认"
            )

    return SafetyCheckResult(allowed=True)


def check_effect_tracking_rule(pond_id: str, device_id: str, action: str,
                               sensor_data_before: Dict[str, float],
                               sensor_data_after: Dict[str, float]) -> bool:
    """检查指令执行效果，判断是否达到预期

    在指令执行后一段时间（EFFECT_CHECK_MINUTES）检查指标是否改善，
    未改善时自动升级告警级别。

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        action: 已执行的动作
        sensor_data_before: 执行前的传感器数据
        sensor_data_after: 执行后的传感器数据

    Returns:
        bool: True=效果符合预期，False=效果未达到预期（需要升级告警）

    效果跟踪规则：
    1. 开增氧机后 15 分钟，DO 应回升至少 1mg/L 或达到 5mg/L 以上
    2. 开水泵后 15 分钟，氨氮应下降至少 0.1mg/L
    3. 开增氧机后 15 分钟，温度应下降至少 1℃
    """
    do_before = sensor_data_before.get("dissolved_oxygen", 0)
    do_after = sensor_data_after.get("dissolved_oxygen", 0)
    ammonia_before = sensor_data_before.get("ammonia_nitrogen", 0)
    ammonia_after = sensor_data_after.get("ammonia_nitrogen", 0)
    temp_before = sensor_data_before.get("temperature", 0)
    temp_after = sensor_data_after.get("temperature", 0)

    if action == "start_aerator":
        if do_before < 5.0:
            if do_after < do_before + 1.0 and do_after < 5.0:
                return False
        if temp_before > 28.0:
            if temp_after >= temp_before:
                return False

    if action == "start_pump":
        if ammonia_before > 0.2:
            if ammonia_after >= ammonia_before - 0.1:
                return False

    return True


def is_high_risk_action(action: str) -> bool:
    """判断是否为高风险动作

    Args:
        action: 动作类型

    Returns:
        bool: True=高风险动作，需要人工确认
    """
    return action in settings.HIGH_RISK_ACTIONS