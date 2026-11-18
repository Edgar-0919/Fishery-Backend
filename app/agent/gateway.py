"""设备下发网关接口（本期只保留接口，不接线）

**闭环边界**：决策链路的终点是 `app/tools/executor.py` 把动作登记进
`device_commands`，`result` 已改为显式的「已登记 / 未接线」文案。
硬件下发不在本期范围内：本模块只定义可替换的网关契约与桩实现，
不改变任何现有执行路径，也不产生设备回执。

接入真实硬件时（OneNET 命令 API / MQTT）需要一次单独评审的最小变更：
1. 实现 ``DeviceGateway.dispatch``；``device_identifier`` 取自
   ``control_devices.device_identifier``（已预留为硬件标识 / MQTT topic）；
2. 在 ``executor.execute_with_check`` 与 ``executor.execute`` 写库前调用
   dispatch，把 ``CommandReceipt.message`` 写入 ``result``，
   并用 ``_UNWIRED_RESULT`` 之外的真实回执替换未接线文案；
3. ``ok=False`` 时不得写 ``status='executed'``、不得翻转
   ``control_devices.status``；接好后恢复 ``EFFECT_CHECK_MINUTES=15``，
   让效果检查重新以设备执行后的水质变化为准。

在此之前，Agent 的「提议 → Guard → 执行器 → 指令登记」链路完整可演示，
`UnwiredGateway` 明确表达「未接线」，`FakeGateway` 供本地联调与契约测试使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class CommandReceipt:
    ok: bool
    message: str
    device_identifier: str = ""
    raw: dict = field(default_factory=dict)


class DeviceGateway(Protocol):
    def dispatch(self, pond_id: str, device_identifier: str,
                 action: str, params: dict) -> CommandReceipt:
        """把一条控制指令下发给真实设备，并返回回执"""
        ...


class FakeGateway:
    """本地联调实现：不联网，返回可区分的模拟回执"""

    name = "fake"

    def dispatch(self, pond_id: str, device_identifier: str,
                 action: str, params: dict) -> CommandReceipt:
        return CommandReceipt(
            ok=True,
            message=f"[FAKE] {action} 已模拟下发（未连接真实设备）",
            device_identifier=device_identifier,
            raw={"pond_id": pond_id, "action": action, "params": params},
        )


class UnwiredGateway:
    """硬件未接线期的默认语义：显式拒绝下发，不产生设备回执

    闭环终点是 ``device_commands`` 的指令登记；本实现存在的意义是让
    「没有硬件」这件事可被检测、可被单测，而不是被静默当作执行成功。
    """

    name = "unwired"

    def dispatch(self, pond_id: str, device_identifier: str,
                 action: str, params: dict) -> CommandReceipt:
        return CommandReceipt(
            ok=False,
            message="[UNWIRED] 硬件网关未接线，指令仅登记至 device_commands",
            device_identifier=device_identifier,
            raw={"pond_id": pond_id, "action": action, "params": params, "wired": False},
        )
