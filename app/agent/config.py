"""Agent LLM 配置 —— 唯一来源为 app.core.config.Settings

历史上本模块自行 load_dotenv + os.getenv，而 Settings 里也声明了同名
LLM_* 字段却从未被使用，导致「改 .env 生效、改配置类不生效」的双源问题。
现在统一由 Settings 提供；保留 load_dotenv 只是为了兼容从非 Backend
目录启动时 pydantic-settings 读不到 .env 的情况。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(backend_dir / ".env")

from app.core.config import settings  # noqa: E402  (需在 load_dotenv 之后导入)

# 环境变量优先：Settings 若在 load_dotenv 之前已被实例化，其 LLM_* 可能仍是默认值
LLM_API_KEY = os.getenv("LLM_API_KEY") or settings.LLM_API_KEY
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or settings.LLM_BASE_URL
LLM_MODEL = os.getenv("LLM_MODEL") or settings.LLM_MODEL
