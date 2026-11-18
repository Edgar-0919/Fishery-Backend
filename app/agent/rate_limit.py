"""全局 LLM 限速器

问答与 Agent 循环原先共抢一个进程级槽位，且 ``time.sleep``
放在锁内执行，导致一条链路的等待会把其它链路全部阻塞。现在：

1. 按链路分桶（chat / agent），互不串行
2. 锁内只计算并「预约」下次可执行时刻，sleep 放到锁外
3. 仍为进程内实现；多实例部署需要 Redis/MySQL 才能做全局配额

间隔由 settings.AGENT_MIN_REQUEST_INTERVAL 控制，测试环境直接放行。
"""

import logging
import os
import threading
import time
from collections import defaultdict

from app.core.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# slot 名称 -> 下一次允许发起调用的时间点（time.monotonic）
_slots: dict[str, float] = defaultdict(float)


def wait_for_slot(slot: str = "default") -> None:
    """在指定链路的槽位上预约一次 LLM 调用（必要时在锁外等待）

    Args:
        slot: 链路标识，例如 "chat" / "decision" / "agent"
    """
    if os.getenv("TEST_MODE", "").lower() in ("1", "true", "yes"):
        return

    interval = max(0.0, float(settings.AGENT_MIN_REQUEST_INTERVAL))
    with _lock:
        now = time.monotonic()
        # 预约而非「事后记录」：并发线程能各自拿到不重叠的时间片
        scheduled = max(now, _slots[slot] + interval)
        _slots[slot] = scheduled

    delay = scheduled - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def reset_for_test() -> None:
    """清空限速状态（仅供测试使用）"""
    with _lock:
        _slots.clear()
