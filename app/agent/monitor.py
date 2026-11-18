"""Agent 决策健康度监控

从 ``agent_runs`` 统计 fallback 占比：LLM 侧整体退化（模型不收敛、tool_calls
失效、持续超时）会表现为 fallback 占比抬升，而设备仍由 ``FALLBACK_ACTIONS``
规则接管、不会停摆 —— 因此这类退化是静默的，必须显式统计并暴露。

统计结果缓存在进程内，由后台定时任务刷新，``/health`` 直接读缓存，
不在请求路径上查库。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from app.core.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_stats: dict = {}
_degraded = False


def _empty() -> dict:
    return {
        "window_minutes": settings.AGENT_FALLBACK_WINDOW_MINUTES,
        "total": 0,
        "fallback": 0,
        "ratio": 0.0,
        "degraded": False,
        "sampled_at": None,
    }


def compute(window_minutes: int | None = None, min_samples: int | None = None,
            ratio_threshold: float | None = None) -> dict:
    """统计窗口内 agent_runs 的 fallback 占比

    Args:
        window_minutes: 统计窗口，默认取配置
        min_samples: 样本量下限；不足时不判定为退化，避免小样本误报
        ratio_threshold: 占比阈值

    Returns:
        dict: total / fallback / ratio / degraded / sampled_at / outcomes

    Note:
        查询失败（表不存在、DB 离线）不改变已有统计，也不抛异常，
        不能因为监控本身让后台调度器挂掉。
    """
    window = int(window_minutes if window_minutes is not None
                 else settings.AGENT_FALLBACK_WINDOW_MINUTES)
    min_samples = int(min_samples if min_samples is not None
                      else settings.AGENT_FALLBACK_MIN_SAMPLES)
    threshold = float(ratio_threshold if ratio_threshold is not None
                      else settings.AGENT_FALLBACK_RATIO_THRESHOLD)

    from app.core.db import execute_mysql

    rows = execute_mysql(
        """
        SELECT outcome, COUNT(*) AS cnt
        FROM agent_runs
        WHERE started_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
        GROUP BY outcome
        """,
        (window,),
    )
    if rows is None:
        with _lock:
            return dict(_last_stats) if _last_stats else _empty()

    counts = {
        str(row.get("outcome") or "unknown"): int(row.get("cnt") or 0)
        for row in rows
    }
    total = sum(counts.values())
    fallback = counts.get("fallback", 0)
    ratio = (fallback / total) if total else 0.0
    degraded = total >= min_samples and ratio >= threshold

    return {
        "window_minutes": window,
        "total": total,
        "fallback": fallback,
        "ratio": round(ratio, 4),
        "degraded": degraded,
        "sampled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "outcomes": counts,
    }


def check() -> dict:
    """刷新统计；只在跨阈值时打日志，避免每轮扫描重复刷屏"""
    global _last_stats, _degraded

    stats = compute()
    with _lock:
        _last_stats = stats
        was_degraded = _degraded
        _degraded = bool(stats["degraded"])

    if stats["degraded"] and not was_degraded:
        logger.warning(
            "[Agent][Monitor] fallback 占比异常: %.1f%% (%s/%s, 窗口 %s 分钟) —— "
            "LLM 侧可能整体退化，设备目前由规则兜底接管",
            stats["ratio"] * 100, stats["fallback"], stats["total"],
            stats["window_minutes"],
        )
    elif was_degraded and not stats["degraded"]:
        logger.info(
            "[Agent][Monitor] fallback 占比恢复: %.1f%% (%s/%s)",
            stats["ratio"] * 100, stats["fallback"], stats["total"],
        )
    return stats


def last_stats() -> dict:
    """读取最近一次统计结果（不查库）"""
    with _lock:
        return dict(_last_stats) if _last_stats else _empty()


def reset_for_test() -> None:
    """清空缓存与阈值状态（仅供测试使用）"""
    global _last_stats, _degraded
    with _lock:
        _last_stats = {}
        _degraded = False
