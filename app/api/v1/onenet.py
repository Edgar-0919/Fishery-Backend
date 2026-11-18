"""OneNET 平台回调接口：URL 验证 + 数据接收

鉴权策略（settings.ONENET_VERIFY_MODE）：
- token（默认）：校验 X-OneNET-Token 头 / Authorization: Bearer / ?token=
- signature：仅校验 MD5 签名（?msg=&nonce=&signature=）
- any：签名或令牌任一通过即可
- none：关闭校验，仅限本地联调

GET 是 OneNET 的 URL 验证流程，固定使用 MD5 签名校验。
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import PlainTextResponse

from app.core.config import settings
from app.api.v1.deps import get_data_processor

logger = logging.getLogger(__name__)

router = APIRouter()

# 推送体大小上限：防止超大 body 拖垮进程
_MAX_BODY_BYTES = 1024 * 1024


def _calc_signature(nonce: str, msg: str) -> str:
    """按 OneNET 规则计算签名：base64(md5(token + nonce + msg))

    Args:
        nonce: 随机串
        msg: 待回显的原始消息

    Returns:
        str: Base64 编码的 MD5 签名
    """
    raw = f"{settings.ONENET_TOKEN}{nonce}{msg}"
    return base64.b64encode(hashlib.md5(raw.encode("utf-8")).digest()).decode("utf-8")


def _verify_signature(signature: str | None, nonce: str | None, msg: str | None) -> bool:
    """校验 OneNET MD5 签名

    Args:
        signature: URL 中的 signature（已 urlencode，需要先解码）
        nonce: 随机串
        msg: 参与签名的消息文本

    Returns:
        bool: 校验是否通过
    """
    if not signature or nonce is None or msg is None:
        return False
    expected = _calc_signature(nonce, msg)
    # 常量时间比较，避免通过响应耗时逐字节试探签名
    return secrets.compare_digest(expected, unquote(signature))


def _extract_token(request: Request, query_token: str | None) -> str:
    """从请求头或 URL 参数中提取平台令牌"""
    token = request.headers.get("X-OneNET-Token") or query_token or ""
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return token


def _authorize(
    request: Request,
    signature: str | None,
    nonce: str | None,
    msg: str | None,
    query_token: str | None,
) -> bool:
    """按配置的校验模式判断请求是否可信

    Args:
        request: 原始请求（读取请求头）
        signature: URL 中的签名
        nonce: 随机串
        msg: 参与签名的消息文本
        query_token: URL 中的 token 参数

    Returns:
        bool: True=放行，False=拒绝
    """
    mode = (settings.ONENET_VERIFY_MODE or "token").strip().lower()

    if mode == "none":
        logger.warning("OneNET 回调校验已关闭（ONENET_VERIFY_MODE=none），仅限本地联调")
        return True

    if _verify_signature(signature, nonce, msg):
        return True

    if mode == "signature":
        return False

    provided = _extract_token(request, query_token)
    return bool(provided) and secrets.compare_digest(provided, settings.ONENET_TOKEN)


@router.get("/callback")
async def verify_url(msg: str = Query(...), nonce: str = Query(...), signature: str = Query(...)):
    if not _verify_signature(signature, nonce, msg):
        logger.warning("OneNET 回调签名验证失败")
        raise HTTPException(status_code=401, detail="验证失败")

    return PlainTextResponse(content=msg)


@router.post("/callback")
async def receive_data(
    request: Request,
    msg: str = Query(None),
    nonce: str = Query(None),
    signature: str = Query(None),
    token: str = Query(None),
):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="请求体过大")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")

    msg_body = body.get("msg") if isinstance(body, dict) else None
    # 签名里的 msg 优先取 query，其次取 body 中的字符串形式
    sign_msg = msg if msg is not None else (msg_body if isinstance(msg_body, str) else None)

    if not _authorize(request, signature, nonce, sign_msg, token):
        client_ip = request.client.host if request.client else "-"
        logger.warning(f"OneNET 回调鉴权失败: ip={client_ip}")
        raise HTTPException(status_code=401, detail="鉴权失败")

    try:
        payload = json.loads(msg_body) if isinstance(msg_body, str) else msg_body
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="msg 字段缺失或格式错误")

        data_processor = get_data_processor()
        # 数据清洗与入库是同步 IO，放到线程池执行，避免阻塞事件循环
        await asyncio.to_thread(data_processor.process_one_message, payload)

        return {"status": "ok"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"OneNET 推送处理失败: {e}")
        return {"status": "error", "msg": str(e)}
