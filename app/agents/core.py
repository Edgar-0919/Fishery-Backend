"""Agent 核心逻辑 —— 封装 LLM 调用，处理问答请求"""
from openai import OpenAI
from app.agents.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from app.agents.prompt import SYSTEM_PROMPT


def _get_client():
    """延迟创建 OpenAI client，避免导入时因缺少 API key 报错"""
    if not LLM_API_KEY:
        raise ValueError("LLM_API_KEY 未配置，请在 .env 文件中设置")
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def chat(user_message: str) -> str:
    """
    最简 Agent 问答：用户消息 + 系统提示词 → LLM → 回答。

    参数:
        user_message: 用户输入的养殖问题

    返回:
        LLM 生成的回答文本
    """
    client = _get_client()
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        max_tokens=2000,
    )
    return response.choices[0].message.content