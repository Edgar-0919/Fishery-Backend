"""GLM / LLM 兼容性验证脚本（开启 Agent 前的门禁，需手动运行）

用法（在 Backend 目录下，且 .env 已配置真实 LLM_API_KEY）：
    python scripts/verify_llm_backend.py

检查三件事：
1. 实际使用的客户端（统一为原生 openai SDK，兼容 OpenAI 协议的端点）
2. 智谱 OpenAI 兼容端点是否正确返回 tool_calls（Agent 循环的前提）
3. 多轮消息中回灌 tool 结果后模型能否正常结束

不通过时不能上线 Agent 决策链路：模型必须支持 tool_calls，
否则循环拿不到任何提议，会整段降级到规则兜底，LLM 分析能力等于失效。
"""

import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))
os.chdir(_BACKEND_DIR)

from app.agent.llm import assistant_message, get_backend, tool_message  # noqa: E402
from app.agent.tools import TOOL_SPECS  # noqa: E402


def main() -> int:
    backend = get_backend()
    print(f"[1/3] 客户端: {backend.name} (model={backend.model})")

    messages = [
        {"role": "system", "content": "你是鱼塘管理员，只能使用提供的工具查询信息。"},
        {"role": "user", "content": "请用 get_device_status 查询 pond1 的设备状态，然后说明结论。"},
    ]

    try:
        response = backend.invoke(messages, tools=TOOL_SPECS)
    except Exception as exc:
        print(f"[2/3] 失败: 首次调用异常 {type(exc).__name__}: {exc}")
        return 1

    print(f"[2/3] tool_calls: {[c.name for c in response.tool_calls]}")
    if not response.tool_calls:
        print("      警告：模型未返回工具调用，请检查该模型/端点是否支持 function calling")
        return 1

    call = response.tool_calls[0]
    messages.append(assistant_message(response))
    messages.append(tool_message(call, '{"ok": true, "data": {"aerator": "online"}}'))

    try:
        final = backend.invoke(messages, tools=TOOL_SPECS)
    except Exception as exc:
        print(f"[3/3] 失败: 多轮回灌异常 {type(exc).__name__}: {exc}")
        return 1

    print(f"[3/3] 多轮回灌成功，回复: {final.content[:120]}")
    print("兼容性验证通过，可以部署 Agent 决策链路")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
