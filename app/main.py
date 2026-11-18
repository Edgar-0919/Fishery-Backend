"""FastAPI 应用入口：创建应用实例、注册路由、配置中间件、管理生命周期"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.api.v1.deps import set_data_processor
from app.api.v1 import alert, auth, chat, device, health, onenet, sensor, task
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.core.security import get_current_user
from app.core.users import ensure_default_admin
from app.services.agent_pipeline import shutdown_decision_executor
from app.services.data_processor import DataProcessor
from app.tools.alert_tracker import start_tracker, stop_tracker

# 日志必须在其他模块产生日志前完成初始化
setup_logging(settings.LOG_LEVEL, settings.LOG_DIR or None)

logger = logging.getLogger(__name__)

# RAG 检索器单例：避免 lifespan 内创建的实例被回收后状态不可查
_rag_retriever = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化数据处理器
    data_processor = DataProcessor()
    set_data_processor(data_processor)

    # 初始化默认管理员账号（仅在 users 表为空时创建）
    ensure_default_admin()

    # 打印回调校验模式：推送方未配置凭据时会被 401 拒绝，日志便于快速定位
    logger.info("OneNET 回调校验模式: %s", settings.ONENET_VERIFY_MODE)
    if settings.ONENET_VERIFY_MODE.strip().lower() == "none":
        logger.warning("OneNET 回调校验已关闭，生产环境请改为 token 或 signature")

    # 初始化 RAG 检索器（预加载 BM25 索引）
    try:
        from app.rag.embeddings import get_embedding_model
        from app.rag.retriever import HybridRetriever

        global _rag_retriever
        _rag_retriever = HybridRetriever()
        _rag_retriever.initialize()
        # 预加载 Embedding 模型（避免首次请求时的等待）
        get_embedding_model()
        logger.info("RAG 检索器初始化完成")
    except Exception as e:
        logger.warning("RAG 初始化跳过（可能尚未安装依赖或构建索引）: %s", e)

    start_tracker()
    yield
    stop_tracker()
    shutdown_decision_executor()


app = FastAPI(
    title="智慧渔业养殖管理系统",
    version="1.0.0",
    lifespan=lifespan,
)

# 使用 Bearer Token 鉴权，不依赖 Cookie，因此关闭 allow_credentials
_cors_origins = [origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 公开接口：健康检查、登录、OneNET 平台回调（回调自带签名校验）
app.include_router(health.router, tags=["Health"])
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(onenet.router, prefix="/onenet", tags=["OneNET"])

# 受保护接口：统一要求 Bearer Token
_protected = [Depends(get_current_user)]
app.include_router(sensor.router, prefix="/api/sensor-data", tags=["Sensor"], dependencies=_protected)
app.include_router(chat.router, prefix="/api/agent", tags=["Agent"], dependencies=_protected)
app.include_router(alert.router, prefix="/api/alerts", tags=["Alert"], dependencies=_protected)
app.include_router(task.router, prefix="/api/manual-tasks", tags=["Task"], dependencies=_protected)
app.include_router(device.router, prefix="/api/control-devices", tags=["Device"], dependencies=_protected)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        log_level="info",
    )
