"""Agent 问答接口：智能对话 + 决策记录查询"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.agent import memory
from app.agent.core import chat_with_sources, iter_chat_events
from app.core.db import execute_mysql
from app.core.security import get_current_user
from app.models.schemas import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_owner(session_id: str | None, user_id: str) -> None:
    """校验会话归属，防止通过猜测 session_id 读取他人对话"""
    if session_id and not memory.is_owned_by(session_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该会话",
        )


@router.post("/chat", response_model=ChatResponse)
async def agent_chat_endpoint(
    req: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """一次性问答接口：保留给非流式调用方与前端降级路径"""
    user_id = current_user["username"]
    _check_owner(req.session_id, user_id)
    try:
        # 同步的 RAG 检索 + LLM 调用放到线程池执行，避免阻塞 asyncio 事件循环，
        # 否则 LLM 慢调用会导致 /api/sensor-data、/onenet/callback 等接口一并卡死
        reply, sources = await asyncio.to_thread(
            chat_with_sources, req.message, req.session_id, user_id
        )
        return ChatResponse(success=True, reply=reply, sources=sources)
    except Exception as e:
        return ChatResponse(success=False, reply=f"Agent 调用失败: {str(e)}", sources=[])


def _format_sse(event: dict) -> str:
    """把结构化事件编码为 SSE 帧（data 统一为单行 JSON，避免多行解析歧义）"""
    payload = json.dumps(event.get("data", {}), ensure_ascii=False)
    return f"event: {event['event']}\ndata: {payload}\n\n"


@router.post("/chat/stream")
async def agent_chat_stream_endpoint(
    req: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """SSE 流式问答：先推送 RAG 参考来源，再逐块推送 Markdown 正文

    生成器是同步的，Starlette 会在线程池中迭代它，因此 RAG 检索与 LLM
    流式读取都不会阻塞事件循环；原有 /chat 接口行为保持不变。
    """
    user_id = current_user["username"]
    _check_owner(req.session_id, user_id)

    def event_stream():
        # 立即吐出一帧注释，避免中间代理缓冲响应头导致前端长时间无响应
        yield ": connected\n\n"
        for event in iter_chat_events(req.message, req.session_id, user_id):
            yield _format_sse(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 关闭 Nginx 等反向代理的缓冲，否则分块会被攒成一整段返回
            "X-Accel-Buffering": "no",
        },
    )


# ---------- 问答会话管理 ----------

@router.post("/sessions")
def create_chat_session(current_user: dict = Depends(get_current_user)):
    """新建会话，返回服务端生成的 session_id"""
    return {
        "success": True,
        "session_id": memory.create_session(current_user["username"]),
    }


@router.get("/sessions")
def list_chat_sessions(
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
    current_user: dict = Depends(get_current_user),
):
    """列出当前用户的会话（标题取第一条提问）"""
    return {
        "success": True,
        "data": memory.list_sessions(current_user["username"], limit),
    }


@router.get("/sessions/{session_id}/messages")
def get_chat_messages(
    session_id: str,
    limit: int = Query(200, ge=1, le=500, description="最多返回的消息条数"),
    current_user: dict = Depends(get_current_user),
):
    """回放会话消息，供前端刷新后恢复界面"""
    user_id = current_user["username"]
    _check_owner(session_id, user_id)
    return {
        "success": True,
        "data": memory.get_messages(session_id, user_id, limit),
    }


@router.delete("/sessions/{session_id}")
def delete_chat_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """删除会话及其全部消息"""
    user_id = current_user["username"]
    _check_owner(session_id, user_id)
    return {"success": memory.delete_session(session_id, user_id)}


@router.get("/decisions")
def get_agent_decisions(
    pond_id: str = Query(None, description="鱼塘ID"),
    limit: int = Query(20, description="返回条数")
):
    """获取 Agent 决策记录

    指令与告警信息均存于 MySQL，采用两步查询后在应用层关联：
    1. 查询 device_commands 获取指令记录
    2. 查询 alert_duration 获取活跃/已升级告警信息
    3. 按 pond_id + alert_type 关联

    Args:
        pond_id: 可选，按鱼塘ID筛选
        limit: 返回条数限制
    """
    try:
        command_columns = """
            id, ts, pond_id, device_id, command_type, trigger_source,
            trigger_alert_id, params, status, executed_at, result
        """

        if pond_id:
            commands = execute_mysql(
                f"SELECT {command_columns} FROM device_commands "
                "WHERE pond_id = %s ORDER BY ts DESC LIMIT %s",
                (pond_id, limit),
            )
            alerts = execute_mysql(
                """
                SELECT pond_id, alert_type, alert_value, threshold_value, agent_decision
                FROM alert_duration
                WHERE status IN ('active', 'escalated') AND pond_id = %s
                """,
                (pond_id,),
            )
        else:
            commands = execute_mysql(
                f"SELECT {command_columns} FROM device_commands "
                "ORDER BY ts DESC LIMIT %s",
                (limit,),
            )
            alerts = execute_mysql(
                """
                SELECT pond_id, alert_type, alert_value, threshold_value, agent_decision
                FROM alert_duration
                WHERE status IN ('active', 'escalated')
                """
            )

        alert_map = {
            f"{row.get('pond_id', '')}_{row.get('alert_type', '')}": row
            for row in (alerts or [])
        }

        data_list = []
        for cmd in commands or []:
            entry = dict(cmd)
            alert_key = f"{cmd.get('pond_id', '')}_{cmd.get('trigger_alert_id', '')}"
            alert = alert_map.get(alert_key)
            entry["alert_type"] = alert.get("alert_type") if alert else None
            entry["alert_value"] = alert.get("alert_value") if alert else None
            entry["threshold_value"] = alert.get("threshold_value") if alert else None
            entry["agent_decision"] = alert.get("agent_decision") if alert else None
            data_list.append(entry)

        return {"success": True, "data": data_list, "count": len(data_list)}
    except Exception as e:
        logger.exception(f"查询决策记录失败: {e}")
        return {"success": False, "data": [], "message": str(e)}
