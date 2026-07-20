"""Pydantic 请求/响应模型（DTO）：定义 API 输入输出数据结构"""

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    success: bool
    reply: str


class SimulatorData(BaseModel):
    device_id: str
    product_id: str = "simulator"
    temperature: float | None = None
    ph_value: float | None = None
    dissolved_oxygen: float | None = None
    ammonia_nitrogen: float | None = None
    nitrite: float | None = None
    ts_value: float | None = None
    water_level: float | None = None


class ResolveTaskRequest(BaseModel):
    handler: str = "admin"
    remark: str = ""


class DeviceCommandRequest(BaseModel):
    command_type: str
    params: dict = {}