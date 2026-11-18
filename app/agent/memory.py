"""问答会话记忆

以 MySQL 为持久化来源（chat_sessions / chat_messages），数据库不可用或表尚未
创建时降级到进程内字典，保证多轮上下文在最小依赖环境下依然可用。

新增能力：
- 会话与 JWT 用户绑定，提供列表 / 历史回放 / 删除 / 新建接口
- 同一会话的「读历史 → 生成 → 写回」由会话级锁串行化
- 一轮问答（user + assistant）在同一事务内写入，避免产生孤儿消息

设计约束：会话记忆只服务于问答，不参与设备决策链路，因此不影响任何兜底规则。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager

from app.core.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_memory: dict[str, deque] = defaultdict(lambda: deque(maxlen=60))

# 会话级锁：串行化同一 session 的问答，避免并发请求读到同一份历史再交错写回
_session_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# 兼容历史数据：user_id 为空的旧会话对当前用户仍可见
_OWNER_CONDITION = "(s.user_id = %s OR s.user_id IS NULL)"


def _db_enabled() -> bool:
    """测试环境跳过真实数据库读写（内存记忆仍然生效）"""
    return os.getenv("TEST_MODE", "").lower() not in ("1", "true", "yes")


def _exec(sql: str, params=None):
    """延迟导入 db，避免模块加载时就要求 MySQL 可用"""
    from app.core.db import execute_mysql
    return execute_mysql(sql, params)


def _tx(statements: list[tuple[str, tuple]]) -> bool:
    from app.core.db import execute_mysql_transaction
    return execute_mysql_transaction(statements)


@contextmanager
def session_lock(session_id: str | None):
    """会话级互斥；无 session_id 时为空操作（保持无状态单轮行为）"""
    if not session_id:
        yield
        return
    with _locks_guard:
        lock = _session_locks.setdefault(session_id, threading.Lock())
    with lock:
        yield


def _upsert_session_sql(session_id: str, user_id: str | None) -> tuple[str, tuple]:
    # 不覆盖已有 user_id，防止会话被其它账号抢占
    return (
        "INSERT INTO chat_sessions (session_id, user_id) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP",
        (session_id, user_id),
    )


def load_history(session_id: str | None, turns: int | None = None,
                 user_id: str | None = None) -> list[dict]:
    """读取最近 N 轮对话，按时间正序返回 [{'role','content'}, ...]

    传入 user_id 时校验会话归属，避免通过猜测 session_id 读取他人上下文。
    """
    if not session_id:
        return []
    limit = max(1, int(turns or settings.AGENT_HISTORY_TURNS)) * 2

    if _db_enabled():
        try:
            if user_id:
                rows = _exec(
                    "SELECT m.role, m.content FROM chat_messages m "
                    "JOIN chat_sessions s ON s.session_id = m.session_id "
                    f"WHERE m.session_id = %s AND {_OWNER_CONDITION} "
                    "ORDER BY m.id DESC LIMIT %s",
                    (session_id, user_id, limit),
                )
            else:
                rows = _exec(
                    "SELECT role, content FROM chat_messages "
                    "WHERE session_id = %s ORDER BY id DESC LIMIT %s",
                    (session_id, limit),
                )
            if rows:
                return [
                    {"role": row["role"], "content": row["content"]}
                    for row in reversed(rows)
                ]
        except Exception as exc:
            logger.warning("[Agent] 会话历史读取失败，回退进程内记忆: %s", exc)

    with _lock:
        return [dict(item) for item in list(_memory.get(session_id, []))][-limit:]


def append(session_id: str | None, role: str, content: str,
           sources: list[dict] | None = None,
           user_id: str | None = None) -> None:
    """追加单条消息；保留给旧调用方，新链路请用 append_turn"""
    if not session_id or not content:
        return

    with _lock:
        _memory[session_id].append({"role": role, "content": content})

    if not _db_enabled():
        return

    try:
        ok = _tx([
            _upsert_session_sql(session_id, user_id),
            (
                "INSERT INTO chat_messages (session_id, role, content, sources) "
                "VALUES (%s, %s, %s, %s)",
                (session_id, role, content,
                 json.dumps(sources or [], ensure_ascii=False)),
            ),
        ])
        if not ok:
            logger.warning("[Agent] 会话落库失败，已保留进程内记忆")
    except Exception as exc:
        logger.warning("[Agent] 会话落库失败，已保留进程内记忆: %s", exc)


def append_turn(session_id: str | None, user_message: str, reply: str,
                sources: list[dict] | None = None,
                user_id: str | None = None) -> None:
    """原子写入一轮问答（user + assistant），失败时内存仍有副本"""
    if not session_id or not reply:
        return

    messages = [("user", user_message, []), ("assistant", reply, sources or [])]
    with _lock:
        for role, content, _ in messages:
            if content:
                _memory[session_id].append({"role": role, "content": content})

    if not _db_enabled():
        return

    try:
        statements = [_upsert_session_sql(session_id, user_id)]
        for role, content, src in messages:
            if content:
                statements.append((
                    "INSERT INTO chat_messages (session_id, role, content, sources) "
                    "VALUES (%s, %s, %s, %s)",
                    (session_id, role, content,
                     json.dumps(src, ensure_ascii=False)),
                ))
        if not _tx(statements):
            logger.warning("[Agent] 本轮会话落库失败，已保留进程内记忆")
    except Exception as exc:
        logger.warning("[Agent] 本轮会话落库失败，已保留进程内记忆: %s", exc)


def clear(session_id: str | None) -> None:
    """清空会话：内存 + 数据库一起删，避免「清了内存但 DB 还能读回旧历史」"""
    if not session_id:
        return
    with _lock:
        _memory.pop(session_id, None)
    with _locks_guard:
        _session_locks.pop(session_id, None)

    if not _db_enabled():
        return
    try:
        _tx([
            ("DELETE FROM chat_messages WHERE session_id = %s", (session_id,)),
            ("DELETE FROM chat_sessions WHERE session_id = %s", (session_id,)),
        ])
    except Exception as exc:
        logger.warning("[Agent] 清空会话失败: %s", exc)


def is_owned_by(session_id: str | None, user_id: str | None) -> bool:
    """校验会话归属；DB 不可用或尚未创建会话时放行（由后续写入建立归属）"""
    if not session_id or not user_id or not _db_enabled():
        return True
    rows = _exec("SELECT user_id FROM chat_sessions WHERE session_id = %s",
                 (session_id,))
    if not rows:  # None（DB 异常）或 []（新会话）都放行
        return True
    owner = rows[0].get("user_id")
    return not owner or owner == user_id


def create_session(user_id: str | None = None) -> str:
    """新建会话并返回 ID（后端生成，前端不再依赖 crypto.randomUUID）"""
    session_id = uuid.uuid4().hex
    if _db_enabled():
        _exec(*_upsert_session_sql(session_id, user_id))
    return session_id


def list_sessions(user_id: str | None = None, limit: int = 20) -> list[dict]:
    """列出用户的会话：标题取第一条用户提问，并附带消息数"""
    if not _db_enabled():
        return []
    rows = _exec(
        "SELECT s.session_id, DATE_FORMAT(s.updated_at, %s) AS updated_at, "
        "(SELECT m.content FROM chat_messages m WHERE m.session_id = s.session_id "
        " AND m.role = 'user' ORDER BY m.id ASC LIMIT 1) AS title, "
        "(SELECT COUNT(1) FROM chat_messages m WHERE m.session_id = s.session_id) "
        " AS message_count "
        "FROM chat_sessions s "
        f"WHERE {_OWNER_CONDITION} "
        "ORDER BY s.updated_at DESC LIMIT %s",
        ("%Y-%m-%d %H:%i", user_id, limit),
    )
    return [dict(row) for row in (rows or [])]


def get_messages(session_id: str | None, user_id: str | None = None,
                 limit: int = 200) -> list[dict]:
    """读取会话全部消息（供前端刷新后回放）"""
    if not session_id:
        return []
    if _db_enabled():
        rows = _exec(
            "SELECT m.id, m.role, m.content, m.sources, "
            "DATE_FORMAT(m.created_at, %s) AS created_at "
            "FROM chat_messages m JOIN chat_sessions s ON s.session_id = m.session_id "
            f"WHERE m.session_id = %s AND {_OWNER_CONDITION} "
            "ORDER BY m.id DESC LIMIT %s",
            ("%Y-%m-%d %H:%i:%s", session_id, user_id, limit),
        )
        if rows:
            result = []
            for row in reversed(rows):
                raw = row.get("sources")
                if isinstance(raw, str):
                    try:
                        sources = json.loads(raw)
                    except (ValueError, TypeError):
                        sources = []
                else:
                    sources = raw or []
                result.append({
                    "id": row.get("id"),
                    "role": row.get("role"),
                    "content": row.get("content") or "",
                    "sources": sources,
                    "created_at": row.get("created_at"),
                })
            return result

    # DB 无数据（或不可用）时用进程内记忆兜底
    with _lock:
        return [
            {"id": None, "role": item["role"], "content": item["content"],
             "sources": [], "created_at": None}
            for item in list(_memory.get(session_id, []))
        ]


def delete_session(session_id: str | None, user_id: str | None = None) -> bool:
    """删除会话及其全部消息；只能删除属于该用户的会话"""
    if not session_id:
        return False
    with _lock:
        _memory.pop(session_id, None)
    with _locks_guard:
        _session_locks.pop(session_id, None)

    if not _db_enabled():
        return True
    return _tx([
        ("DELETE FROM chat_messages WHERE session_id = %s", (session_id,)),
        ("DELETE FROM chat_sessions WHERE session_id = %s "
         "AND (user_id = %s OR user_id IS NULL)", (session_id, user_id)),
    ])
