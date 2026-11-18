"""Pydantic 请求/响应模型（DTO）：定义 API 输入输出数据结构"""

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    # 会话ID：为空时保持无状态单轮行为，传入后启用多轮上下文记忆
    session_id: str | None = None


class ChatResponse(BaseModel):
    success: bool
    reply: str
    # RAG 检索到的参考知识来源列表（来源路径 + 相似度 + 内容片段）
    sources: list[dict] = []


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
    # 人工下发时的操作说明，写入 device_commands.reason，便于回溯
    reason: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    success: bool
    token: str = ""
    user: dict = {}
    message: str = ""


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str