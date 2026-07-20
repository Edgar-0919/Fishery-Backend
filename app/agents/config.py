"""Agent 配置模块 —— 从 .env 文件读取 LLM 配置"""
import os
from dotenv import load_dotenv
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(backend_dir / ".env")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")