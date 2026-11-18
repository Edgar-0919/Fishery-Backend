"""Agent 决策审计

写库失败绝不影响处置：始终先写入进程内环形缓冲（便于本地排查与测试断言），
再尝试落 MySQL。表不存在、数据库离线时只记 warning。
"""

from __future__ import annotations

import logging
import os
from collections import deque

from app.agent.proposals import AgentRun

logger = logging.getLogger(__name__)

_recent: deque[AgentRun] = deque(maxlen=200)

_INSERT_SQL = """
    INSERT INTO agent_runs
    (trace_id, pond_id, device_id, alert_type, mode, started_at, finished_at,
     steps, prompt_tokens, completion_tokens, latency_ms, outcome,
     fallback_reason, proposals, rag_queries, final_actions, error)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def write(run: AgentRun) -> None:
    """记录一次决策；任何异常都被吞掉，只影响审计不影响处置"""
    _recent.append(run)

    if os.getenv("TEST_MODE", "").lower() in ("1", "true", "yes"):
        return

    try:
        from app.core.db import execute_mysql

        row = run.to_row()
        execute_mysql(_INSERT_SQL, (
            row["trace_id"], row["pond_id"], row["device_id"], row["alert_type"],
            row["mode"], row["started_at"], row["finished_at"], row["steps"],
            row["prompt_tokens"], row["completion_tokens"], row["latency_ms"],
            row["outcome"], row["fallback_reason"], row["proposals"],
            row["rag_queries"], row["final_actions"], row["error"],
        ))
    except Exception as exc:
        logger.warning("[Agent] 审计写库失败（不影响处置）trace=%s: %s", run.trace_id, exc)


def recent_runs() -> list[AgentRun]:
    """返回最近的审计记录（测试与本地排查用）"""
    return list(_recent)


def clear_recent() -> None:
    _recent.clear()
