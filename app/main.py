"""FastAPI 应用入口：创建应用实例、注册路由、配置中间件、管理生命周期"""

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.api.v1.endpoints import onenet, sensor, chat, alert, task, device
from app.api.v1.deps import set_data_processor
from app.services.data_processor import DataProcessor
from app.tools.alert_tracker import start_tracker, stop_tracker


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_processor = DataProcessor()
    set_data_processor(data_processor)
    start_tracker()
    yield
    stop_tracker()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(onenet.router, prefix="/onenet", tags=["OneNET"])
app.include_router(sensor.router, prefix="/api/sensor-data", tags=["Sensor"])
app.include_router(chat.router, prefix="/api/agent", tags=["Agent"])
app.include_router(alert.router, prefix="/api/alerts", tags=["Alert"])
app.include_router(task.router, prefix="/api/manual-tasks", tags=["Task"])
app.include_router(device.router, prefix="/api/control-devices", tags=["Device"])


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")