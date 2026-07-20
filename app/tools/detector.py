"""异常检测引擎：阈值越界检测 + 趋势预警，从 config 读取阈值配置

核心功能：
1. 阈值越界检测：根据 settings.ALERT_THRESHOLDS 判断当前值是否超出安全范围
2. 趋势预警：检测连续 3 个采样点的变化趋势，即使未越界也会触发预警
3. 支持两种严重级别：critical（越界）和 warning（接近阈值或趋势异常）

阈值配置结构（来自 settings.ALERT_THRESHOLDS）：
{
    'sensor_type': {
        'min': 下限临界值,
        'max': 上限临界值,
        'warning_min': 下限预警值（可选）,
        'warning_max': 上限预警值（可选）
    }
}

告警类型命名规则：
- {sensor_type}_low → 低于下限临界值（critical）
- {sensor_type}_high → 高于上限临界值（critical）
- {sensor_type}_low_warning → 低于下限预警值（warning）
- {sensor_type}_high_warning → 高于上限预警值（warning）
- {sensor_type}_trend_rising → 持续上升趋势（warning）
- {sensor_type}_trend_falling → 持续下降趋势（warning）
"""

from collections import deque
from typing import Optional

from app.models.domain import AlertEvent
from app.core.config import settings


def _detect_trend(history: deque) -> str:
    """检测传感器数值的变化趋势

    使用最近 3 个采样点判断趋势：
    - 连续上升：values[0] < values[1] < values[2] → "rising"
    - 连续下降：values[0] > values[1] > values[2] → "falling"
    - 其他情况 → "stable"

    Args:
        history: 包含 (时间戳ms, 值) 元组的双端队列

    Returns:
        str: "rising"（上升）、"falling"（下降）或 "stable"（稳定）

    Note:
        历史数据不足 3 条时直接返回 "stable"，避免误判
    """
    if len(history) < 3:
        return "stable"
    recent = list(history)[-3:]
    values = [v for _, v in recent]
    if values[0] < values[1] < values[2]:
        return "rising"
    elif values[0] > values[1] > values[2]:
        return "falling"
    return "stable"


def check(pond_id: str, device_id: str, sensor_type: str, value: float,
          history: deque) -> Optional[AlertEvent]:
    """检测单个传感器值是否触发告警

    检测优先级（按顺序判断，命中即返回）：
    1. 低于下限临界值 → critical
    2. 高于上限临界值 → critical
    3. 低于下限预警值 → warning
    4. 高于上限预警值 → warning
    5. 连续趋势变化（未越界但持续上升/下降）→ warning
    6. 无异常 → None

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        sensor_type: 传感器类型（如 temperature, dissolved_oxygen）
        value: 当前传感器数值
        history: 该传感器的历史数据滑动窗口

    Returns:
        Optional[AlertEvent]: 告警事件对象，未触发告警时返回 None

    Note:
        warning_min/warning_max 为可选配置，仅部分指标有预警阈值
        trend 字段用于后续 Agent 决策时参考变化方向
    """
    threshold = settings.ALERT_THRESHOLDS.get(sensor_type)
    if not threshold:
        return None

    trend = _detect_trend(history)

    if value < threshold["min"]:
        return AlertEvent(
            pond_id=pond_id, device_id=device_id,
            sensor_type=sensor_type, value=value,
            threshold=threshold["min"], severity="critical",
            trend=trend, alert_type=f"{sensor_type}_low",
            message=f"{sensor_type}过低: {value} < {threshold['min']}"
        )
    if value > threshold["max"]:
        return AlertEvent(
            pond_id=pond_id, device_id=device_id,
            sensor_type=sensor_type, value=value,
            threshold=threshold["max"], severity="critical",
            trend=trend, alert_type=f"{sensor_type}_high",
            message=f"{sensor_type}过高: {value} > {threshold['max']}"
        )

    if "warning_min" in threshold and value < threshold["warning_min"]:
        return AlertEvent(
            pond_id=pond_id, device_id=device_id,
            sensor_type=sensor_type, value=value,
            threshold=threshold["warning_min"], severity="warning",
            trend=trend, alert_type=f"{sensor_type}_low_warning",
            message=f"{sensor_type}偏低: {value} < {threshold['warning_min']}"
        )
    if "warning_max" in threshold and value > threshold["warning_max"]:
        return AlertEvent(
            pond_id=pond_id, device_id=device_id,
            sensor_type=sensor_type, value=value,
            threshold=threshold["warning_max"], severity="warning",
            trend=trend, alert_type=f"{sensor_type}_high_warning",
            message=f"{sensor_type}偏高: {value} > {threshold['warning_max']}"
        )

    if trend in ("rising", "falling"):
        direction = "上升" if trend == "rising" else "下降"
        return AlertEvent(
            pond_id=pond_id, device_id=device_id,
            sensor_type=sensor_type, value=value,
            threshold=0, severity="warning",
            trend=trend, alert_type=f"{sensor_type}_trend_{trend}",
            message=f"{sensor_type}持续{direction}: 当前值 {value}（未越界但趋势预警）"
        )

    return None


def check_all(pond_id: str, device_id: str, data: dict[str, float],
              history_dict: dict[str, deque]) -> list[AlertEvent]:
    """批量检测多个传感器值

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        data: {sensor_type: value} 字典
        history_dict: 历史数据字典，key 格式为 "{pond_id}:{device_id}:{sensor_type}"

    Returns:
        list[AlertEvent]: 按严重级别排序的告警列表（critical 优先）

    Note:
        返回列表按 severity 排序，critical 级别告警排在前面
        便于上层优先处理严重告警
    """
    alerts = []
    for sensor_type, value in data.items():
        key = f"{pond_id}:{device_id}:{sensor_type}"
        history = history_dict.get(key, deque())
        alert = check(pond_id, device_id, sensor_type, value, history)
        if alert:
            alerts.append(alert)

    alerts.sort(key=lambda a: (0 if a.severity == "critical" else 1))
    return alerts