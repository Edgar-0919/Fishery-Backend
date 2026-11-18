"""统一 LLM 调用层

项目只有这一条 LLM 调用路径：问答（``core.py``）与 Agent 循环（``loop.py``）
全部走这里的 ``invoke`` / ``stream``。

客户端固定使用原生 ``openai`` SDK（OpenAI 兼容端点，当前为智谱 GLM）。
不使用 LangChain / LangGraph 的编排能力：``create_agent`` / ``ToolNode`` 会在模型
返回 tool_call 时立即执行工具，绕过「LLM 只提议、Guard 统一执行」的安全边界；
工具的执行时机必须由 ``app/agent/loop.py`` 掌握。

``tools`` 与 ``model`` 之外的参数由调用方显式传入（temperature / max_tokens /
timeout / slot），保证问答与 Agent 两条链路的行为差异可见、可追溯。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)

from app.agent.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from app.agent.rate_limit import wait_for_slot
from app.core.config import settings

logger = logging.getLogger(__name__)

_TRANSIENT_ERRORS = (APITimeoutError, APIConnectionError, APIStatusError)


class LLMError(Exception):
    """LLM 调用失败的统一基类

    上层（tenacity 重试、Agent 规则兜底）只依赖这一层，不感知 openai SDK 的
    异常类型。将来更换客户端实现时，只需在 ``_translate`` 里补映射。
    """


class LLMNotConfiguredError(LLMError):
    """LLM_API_KEY 未配置，无法发起调用"""


class LLMTimeoutError(LLMError):
    """单次调用超时"""


class LLMConnectionError(LLMError):
    """网络不可达 / 连接中断"""


class LLMStatusError(LLMError):
    """服务端返回 4xx / 5xx"""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def assistant_message(resp: LLMResponse) -> dict:
    """把模型回复转成统一的 OpenAI 格式消息，供下一轮循环回灌"""
    message: dict[str, Any] = {"role": "assistant", "content": resp.content}
    if resp.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.args, ensure_ascii=False),
                },
            }
            for call in resp.tool_calls
        ]
    return message


def tool_message(call: ToolCall, result: str) -> dict:
    return {"role": "tool", "tool_call_id": call.id, "content": result}


def _translate(exc: Exception) -> LLMError:
    """把 openai SDK 异常收敛成统一异常（保留原异常作为 __cause__）"""
    if isinstance(exc, APITimeoutError):
        return LLMTimeoutError(str(exc))
    if isinstance(exc, APIConnectionError):
        return LLMConnectionError(str(exc))
    if isinstance(exc, APIStatusError):
        return LLMStatusError(str(exc))
    return LLMError(str(exc))


def _extract_delta(chunk) -> str:
    """从流式 chunk（SDK 对象或兼容字典）中安全取出增量文本"""
    if isinstance(chunk, dict):
        choices = chunk.get("choices") or []
        first = choices[0] if choices else {}
        return (first.get("delta") or {}).get("content") or ""
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    return getattr(delta, "content", None) or ""


class LLMClient:
    """原生 openai SDK 客户端（项目唯一的 LLM 后端）"""

    name = "openai-sdk"

    def __init__(self) -> None:
        kwargs: dict[str, Any] = {
            # 用占位符让构造永远成功；未配置 Key 的失败留到调用时，
            # 由问答的错误提示 / 决策的规则兜底 / Agent 的降级分别处理
            "api_key": LLM_API_KEY or "not-configured",
        }
        if LLM_BASE_URL:
            kwargs["base_url"] = LLM_BASE_URL
        self._client = OpenAI(**kwargs)
        logger.info("[LLM] 后端: 原生 openai SDK (model=%s)", LLM_MODEL)

    @property
    def model(self) -> str:
        return LLM_MODEL

    def ensure_configured(self) -> None:
        """显式校验 API Key；需要「未配置」与「调用失败」区分处理的调用方使用"""
        if not LLM_API_KEY:
            raise LLMNotConfiguredError("LLM_API_KEY 未配置，请在 .env 文件中设置")

    def _timeout(self, timeout: float | None) -> float:
        """单次调用超时：不超过全局 LLM_TIMEOUT，Agent 侧还会再收紧到剩余预算"""
        if timeout is None:
            return float(settings.LLM_TIMEOUT)
        return min(float(settings.LLM_TIMEOUT), float(timeout))

    def invoke(self, messages: list[dict], tools: list[dict] | None = None,
               timeout: float | None = None, *,
               temperature: float = 0.1,
               max_tokens: int | None = None,
               slot: str = "agent") -> LLMResponse:
        """单次（非流式）调用，返回统一结构

        Args:
            messages: OpenAI 格式消息列表
            tools: 标准 OpenAI function schema 列表，None 表示不启用工具
            timeout: 调用方给定的超时上限，实际取 min(LLM_TIMEOUT, timeout)
            temperature: 采样温度，各链路按自身场景显式传入
            max_tokens: 输出上限，None 表示不下发，交给模型默认值
            slot: 限速槽位（chat / agent）
        """
        # 缺 Key 时立即失败，避免发出必然 401 的请求再叠加重试等待
        self.ensure_configured()
        wait_for_slot(slot)
        kwargs: dict[str, Any] = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": temperature,
            "timeout": self._timeout(timeout),
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools

        try:
            response = self._client.chat.completions.create(**kwargs)
        except _TRANSIENT_ERRORS as exc:
            raise _translate(exc) from exc

        message = response.choices[0].message
        calls = []
        for index, raw in enumerate(message.tool_calls or []):
            try:
                args = json.loads(raw.function.arguments or "{}")
            except (TypeError, ValueError):
                args = {}
            calls.append(ToolCall(id=raw.id or str(index),
                                  name=raw.function.name, args=args))

        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=message.content or "",
            tool_calls=calls,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )

    def stream(self, messages: list[dict], timeout: float | None = None, *,
               temperature: float = 0.3,
               max_tokens: int | None = None,
               slot: str = "chat") -> Iterator[str]:
        """流式调用，逐段产出文本增量

        流式不做自动重试：已推送给前端的文本无法撤回，重试会造成内容重复。
        失败时由调用方保留已生成部分并推送 error 事件。
        """
        self.ensure_configured()
        wait_for_slot(slot)
        kwargs: dict[str, Any] = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": temperature,
            "timeout": self._timeout(timeout),
            "stream": True,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        try:
            for chunk in self._client.chat.completions.create(**kwargs):
                text = _extract_delta(chunk)
                if text:
                    yield text
        except _TRANSIENT_ERRORS as exc:
            raise _translate(exc) from exc


_backend: LLMClient | None = None
_backend_lock = threading.Lock()


def get_backend() -> LLMClient:
    """延迟构造单例客户端；导入与启动永不因缺少 Key 失败"""
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = LLMClient()
    return _backend


def reset_backend() -> None:
    """清空客户端单例（仅供测试使用）"""
    global _backend
    with _backend_lock:
        _backend = None


def invoke(messages: list[dict], tools: list[dict] | None = None,
           timeout: float | None = None, **kwargs) -> LLMResponse:
    """模块级便捷入口，转发到单例客户端"""
    return get_backend().invoke(messages, tools, timeout, **kwargs)


def stream(messages: list[dict], timeout: float | None = None,
           **kwargs) -> Iterator[str]:
    """模块级流式入口，转发到单例客户端"""
    return get_backend().stream(messages, timeout, **kwargs)


def ensure_configured() -> None:
    """校验 API Key 是否已配置"""
    get_backend().ensure_configured()


# 兼容既有导入名；实现已统一，不再有第二个后端
OpenAIBackend = LLMClient
