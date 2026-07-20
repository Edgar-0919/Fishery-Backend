"""领域实体：业务领域中的核心数据结构"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class AlertEvent:
    pond_id: str
    device_id: str
    sensor_type: str
    value: float
    threshold: float
    severity: str
    trend: str
    alert_type: str
    message: str