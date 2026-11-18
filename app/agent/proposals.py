"""Agent 数据结构：动作提议、运行状态、审计记录

``ActionProposal`` 是 Guard 的第一道防线：动作名用 Literal 枚举，幻觉动作
（如 drop_table）在 Pydantic 构造阶段就会抛 ValidationError，由 loop 统一
降级到规则兜底，不会流入执行器。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ActionName = Literal[
    "start_aerator", "stop_aerator",
    "start_pump", "stop_pump",
    "start_feeder", "stop_feeder",
    "alert_human", "no_action",
]

ALLOWED_ACTIONS: tuple[str, ...] = (
    "start_aerator", "stop_aerator",
    "start_pump", "stop_pump",
    "start_feeder", "stop_feeder",
    "alert_human", "no_action",
)

MAX_ACTION_DURATION = 120
_ALLOWED_POWER = {"low", "medium", "high"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class ActionProposal(BaseModel):
    """LLM 提出的一个待执行动作（只提议，不执行）"""

    action: ActionName
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("params")
    @classmethod
    def _check_params(cls, value: dict) -> dict:
        duration = value.get("duration")
        if duration is not None:
            try:
                duration = float(duration)
            except (TypeError, ValueError) as exc:
                raise ValueError("duration 必须是数值") from exc
            if not 0 < duration <= MAX_ACTION_DURATION:
                raise ValueError(f"duration 超出允许范围 1~{MAX_ACTION_DURATION}")

        power = value.get("power")
        if power is not None and str(power).lower() not in _ALLOWED_POWER:
            raise ValueError("power 仅允许 low/medium/high")

        for key, item in value.items():
            if not isinstance(item, (str, int, float, bool, type(None))):
                raise ValueError(f"参数 {key} 的类型不受支持")
        return value


@dataclass
class AgentState:
    """单次 Agent 运行的内存状态（不持久化，长时程状态仍以业务表为准）"""

    alert: Any
    context: dict[str, Any]
    messages: list[dict] = field(default_factory=list)
    proposals: list[ActionProposal] = field(default_factory=list)
    rag_queries: list[dict] = field(default_factory=list)
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class AgentRun:
    """一次决策的完整审计记录，对应 agent_runs 表一行"""

    mode: str = "react"
    pond_id: str = ""
    device_id: str = ""
    alert_type: str = ""
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    outcome: str = "pending"  # executed / blocked / fallback / error / skipped
    fallback_reason: str = ""
    proposals: list[dict] = field(default_factory=list)
    rag_queries: list[dict] = field(default_factory=list)
    final_actions: list[dict] = field(default_factory=list)
    error: str = ""
    _start: float = field(default_factory=time.monotonic, repr=False)

    @classmethod
    def new(cls, mode: str, alert: Any) -> "AgentRun":
        return cls(
            mode=mode,
            pond_id=getattr(alert, "pond_id", ""),
            device_id=getattr(alert, "device_id", ""),
            alert_type=getattr(alert, "alert_type", ""),
        )

    def finish(self, state: AgentState | None = None) -> None:
        self.finished_at = _now()
        self.latency_ms = int((time.monotonic() - self._start) * 1000)
        if state is not None:
            self.steps = state.steps
            self.prompt_tokens = state.input_tokens
            self.completion_tokens = state.output_tokens

    def to_row(self) -> dict:
        """转成 agent_runs 表的字段字典（JSON 列序列化为字符串）"""
        return {
            "trace_id": self.trace_id,
            "pond_id": self.pond_id,
            "device_id": self.device_id,
            "alert_type": self.alert_type,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at or None,
            "steps": self.steps,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
            "outcome": self.outcome,
            "fallback_reason": self.fallback_reason[:255],
            "proposals": json.dumps(self.proposals, ensure_ascii=False),
            "rag_queries": json.dumps(self.rag_queries, ensure_ascii=False),
            "final_actions": json.dumps(self.final_actions, ensure_ascii=False),
            "error": self.error[:1000],
        }
