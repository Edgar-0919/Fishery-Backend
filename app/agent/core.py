"""Agent 核心逻辑 —— 封装 LLM 调用，处理问答请求（含 RAG 检索注入）

支持超时控制、重试机制和速率限制，确保 LLM 调用稳定性。
当 RAG_ENABLED=true 时，自动检索知识库并将相关知识注入 system prompt。

LLM 调用统一走 ``app.agent.llm``，与决策、Agent 循环共用同一个客户端。
"""
import logging
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.agent import llm
from app.agent.llm import LLMConnectionError, LLMStatusError, LLMTimeoutError
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.memory import append_turn, load_history, session_lock
from app.agent.context_budget import format_rag_context, trim_history
from app.core.config import settings

logger = logging.getLogger(__name__)


def _format_rag_context(results: list[dict]) -> str:
    """问答侧 RAG 上下文；实现收拢到 context_budget，保留此名兼容既有调用方"""
    return format_rag_context(
        results, "（暂无相关参考知识，请基于通用养殖知识回答）"
    )


def _retrieve_knowledge(query: str) -> list[dict]:
    """RAG 检索：从知识库中检索相关养殖知识

    Args:
        query: 检索查询文本（用户问题）

    Returns:
        list[dict]: 检索结果，按相似度降序排列
    """
    try:
        from app.rag.retriever import retrieve
        return retrieve(query, top_k=settings.RAG_TOP_K,
                        score_threshold=settings.RAG_SCORE_THRESHOLD)
    except Exception as e:
        logger.warning(f"RAG 检索失败，回退到无 RAG 模式: {e}")
        return []


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=1, max=10),
    retry=retry_if_exception_type((LLMTimeoutError, LLMConnectionError, LLMStatusError)),
    reraise=True
)
def _call_llm_with_retry(messages: list[dict]):
    """带重试的问答调用；限速槽位与超时由统一客户端负责"""
    return llm.invoke(
        messages,
        temperature=0.3,
        max_tokens=2000,
        timeout=settings.LLM_TIMEOUT,
        slot="chat",
    )


def chat(user_message: str) -> str:
    """
    Agent 问答：用户消息 + 系统提示词 + RAG 检索知识 → LLM → 回答。

    支持 settings.LLM_TIMEOUT 超时、重试（2次，指数退避）和速率限制。
    当 RAG_ENABLED=true 时，自动检索知识库并注入相关知识。

    参数:
        user_message: 用户输入的养殖问题

    返回:
        LLM 生成的回答文本，调用失败时返回错误提示
    """
    reply, _sources = chat_with_sources(user_message)
    return reply


def _build_chat_context(user_message: str,
                        session_id: str | None = None,
                        user_id: str | None = None) -> tuple[list, list[dict]]:
    """准备一次问答所需的消息列表与参考来源（流式/非流式共用）

    在这里显式校验 API Key，让「未配置」与「调用失败」走出不同的用户提示。
    """
    llm.ensure_configured()

    # 构建增强 system prompt
    enhanced_prompt = SYSTEM_PROMPT
    rag_results: list[dict] = []
    if settings.RAG_ENABLED:
        rag_results = _retrieve_knowledge(user_message)
        context_text = _format_rag_context(rag_results)
        enhanced_prompt = f"{SYSTEM_PROMPT}\n\n## 参考知识\n{context_text}"

    # 多轮上下文：system → 按字符预算裁剪的历史 → 当前问题
    # RAG 知识每轮重新检索并放进 system，不会残留在历史消息中
    history, dropped = trim_history(load_history(session_id, user_id=user_id))
    messages = [{"role": "system", "content": enhanced_prompt}]
    if dropped:
        messages[0]["content"] += (
            f"\n\n（为控制上下文长度，已省略更早的 {dropped} 条对话记录）"
        )
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 格式化为前端可用的来源结构：附带召回片段原文供界面展开查看。
    # 顺序与 prompt 中 [来源N] 一致（prompt 侧可能因字符预算截断）。
    sources = [
        {
            "source": r.get("source", ""),
            "score": r.get("score", 0),
            "category": r.get("category", ""),
            "title": r.get("title", ""),
            "content": r.get("content", ""),
        }
        for r in rag_results
    ]
    return messages, sources


def chat_with_sources(user_message: str,
                      session_id: str | None = None,
                      user_id: str | None = None) -> tuple[str, list[dict]]:
    """Agent 问答：返回回答文本 + RAG 检索到的参考来源（支持多轮会话）

    与 chat() 的区别在于额外返回 RAG 检索结果，供前端展示来源追溯。
    传入 session_id 时读取最近 N 轮历史，并把本轮问答写回会话；
    不传时保持原有的无状态单轮行为，完全兼容旧调用方。

    参数:
        user_message: 用户输入的养殖问题
        session_id: 会话ID，可为空
        user_id: 当前登录用户，用于会话归属校验与落库

    返回:
        (reply, sources)：
        - reply: LLM 生成的回答文本
        - sources: RAG 检索结果列表 [{source, score, category, title}]
    """
    # 会话级串行：避免并发请求读到同一份历史后交错写回
    with session_lock(session_id):
        return _chat_once(user_message, session_id, user_id)


def _chat_once(user_message: str, session_id: str | None,
               user_id: str | None) -> tuple[str, list[dict]]:
    try:
        messages, sources = _build_chat_context(
            user_message, session_id, user_id
        )
    except Exception as e:
        logger.error("LLM 客户端初始化失败: %s", e)
        return "抱歉，AI 服务暂未正确配置，请稍后重试。", []

    try:
        response = _call_llm_with_retry(messages)
        reply = response.content
        if session_id:
            append_turn(session_id, user_message, reply, sources, user_id)
        return reply, sources
    except (LLMTimeoutError, LLMConnectionError, LLMStatusError) as e:
        logger.error(f"问答 LLM 调用失败: {e}")
        return f"抱歉，AI 服务暂时不可用（{type(e).__name__}），请稍后重试。", sources
    except Exception as e:
        logger.exception(f"问答 LLM 调用异常: {e}")
        return f"抱歉，处理您的请求时出现错误，请稍后重试。", sources


def iter_chat_events(user_message: str, session_id: str | None = None,
                     user_id: str | None = None):
    """流式问答事件生成器（同步，由 StreamingResponse 放到线程池迭代）

    依次产出结构化事件：
    - meta:  {"sources": [...]}  RAG 参考来源，先于正文到达
    - delta: {"content": "..."}  LLM 增量文本
    - error: {"message": "..."}  生成失败提示
    - done:  {"ok": bool}        结束标记

    说明：流式调用不做自动重试，因为已推送给前端的文本无法撤回；
    失败时保留已生成内容并通过 error 事件提示，和非流式接口的错误文案保持一致。
    会话锁在整个生成期间持有，客户端断开、生成器被关闭时会自动释放。
    """
    with session_lock(session_id):
        yield from _iter_chat_events(user_message, session_id, user_id)


def _iter_chat_events(user_message: str, session_id: str | None,
                      user_id: str | None):
    try:
        messages, sources = _build_chat_context(
            user_message, session_id, user_id
        )
    except Exception as e:
        logger.error("LLM 客户端初始化失败: %s", e)
        yield {"event": "error", "data": {"message": "抱歉，AI 服务暂未正确配置，请稍后重试。"}}
        yield {"event": "done", "data": {"ok": False}}
        return

    yield {"event": "meta", "data": {"sources": sources}}

    parts: list[str] = []
    error_message = None
    try:
        for text in llm.stream(
            messages,
            temperature=0.3,
            max_tokens=2000,
            timeout=settings.LLM_TIMEOUT,
            slot="chat",
        ):
            parts.append(text)
            yield {"event": "delta", "data": {"content": text}}
    except (LLMTimeoutError, LLMConnectionError, LLMStatusError) as e:
        logger.error("流式问答 LLM 调用失败: %s", e)
        error_message = f"抱歉，AI 服务暂时不可用（{type(e).__name__}），请稍后重试。"
    except Exception as e:
        logger.exception("流式问答 LLM 调用异常: %s", e)
        error_message = "抱歉，处理您的请求时出现错误，请稍后重试。"

    # 已生成的部分同样写入会话，避免多轮上下文与用户看到的内容不一致
    reply = "".join(parts)
    if session_id and reply:
        append_turn(session_id, user_message, reply, sources, user_id)

    if error_message:
        yield {"event": "error", "data": {"message": error_message}}
    yield {"event": "done", "data": {"ok": error_message is None}}