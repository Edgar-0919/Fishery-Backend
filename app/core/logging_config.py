"""日志配置：统一格式、控制台 + 滚动文件双输出

使用方式：
    from app.core.logging_config import setup_logging
    setup_logging()                      # 应用启动时调用一次
    logger = logging.getLogger(__name__)  # 各模块按需获取

日志格式包含时间、级别、模块名，便于按模块过滤决策链路。
文件日志按 10MB 滚动，保留 5 份，避免长期运行占满磁盘。
"""

import logging
import logging.handlers
import sys
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-30s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

_configured = False


def _default_log_dir() -> Path:
    # app/core/logging_config.py -> Backend/logs
    return Path(__file__).resolve().parent.parent.parent / "logs"


def setup_logging(level: str = "INFO", log_dir: str | None = None) -> None:
    """初始化全局日志（重复调用无副作用）

    Args:
        level: 日志级别字符串
        log_dir: 日志目录，为空时使用 Backend/logs
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

    # 控制台输出：本地运行时直接输出到终端，或由进程管理器收集
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # 文件输出：滚动保留，便于事后追溯决策过程
    target_dir = Path(log_dir) if log_dir else _default_log_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            target_dir / "fishery.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        # 目录不可写时降级为仅控制台，不影响服务启动
        root.warning("文件日志初始化失败，仅输出到控制台: %s", exc)

    # 降低第三方库噪音，只保留告警以上
    for noisy_logger in (
        "httpx",
        "httpcore",
        "urllib3",
        "chromadb",
        "sentence_transformers",
        "transformers",
        "apscheduler",
    ):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    _configured = True


def is_configured() -> bool:
    """是否已完成初始化（供测试判断）"""
    return _configured
