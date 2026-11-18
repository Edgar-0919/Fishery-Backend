"""认证接口：登录、获取当前用户、修改密码

除登录外，其余接口均需要 Bearer Token。
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.db import execute_mysql
from app.core.security import create_access_token, get_current_user
from app.core.users import authenticate, get_user, update_password
from app.models.schemas import ChangePasswordRequest, LoginRequest, LoginResponse

router = APIRouter()

# 密码最小长度
_MIN_PASSWORD_LENGTH = 6


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    """用户名密码登录，成功后返回 JWT"""
    user = authenticate(req.username, req.password)
    if not user:
        # 区分「数据库不可用」与「密码错误」，避免运维误判
        if execute_mysql("SELECT 1 AS ok") is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="认证服务暂不可用，请检查数据库连接",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    token = create_access_token(user["username"], user["role"])
    return LoginResponse(success=True, token=token, user=user)


@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    """获取当前登录用户信息"""
    user = get_user(current_user["username"])
    if not user or not user.get("is_active"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号不存在或已停用",
        )

    return {
        "success": True,
        "user": {
            "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "role": user.get("role") or "operator",
        },
    }


@router.post("/change-password")
def change_password(
    req: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
):
    """修改当前用户密码"""
    if len(req.new_password) < _MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"新密码至少 {_MIN_PASSWORD_LENGTH} 位",
        )

    user = authenticate(current_user["username"], req.old_password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="原密码不正确",
        )

    if not update_password(current_user["username"], req.new_password):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="密码更新失败，请稍后重试",
        )

    return {"success": True, "message": "密码已更新，请使用新密码重新登录"}
