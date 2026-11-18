"""JWT 签权与密码哈希

设计说明：
- 密码使用标准库 hashlib.pbkdf2_hmac 加盐哈希，不引入 passlib/bcrypt 额外依赖
- Token 使用 HS256 对称签名，密钥来自 .env 的 JWT_SECRET
- 所有需要鉴权的路由通过 Depends(get_current_user) 统一保护
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

_ALGORITHM = "HS256"
_PBKDF2_ITERATIONS = 120_000

# auto_error=False：未携带凭证时返回 None，由依赖函数给出统一的中文 401 提示
_bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """生成密码哈希

    Args:
        password: 明文密码

    Returns:
        str: 格式为 pbkdf2_sha256$迭代次数$盐$哈希值
    """
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """校验密码是否匹配存储的哈希

    Args:
        password: 明文密码
        stored_hash: 数据库中保存的哈希字符串

    Returns:
        bool: 是否匹配
    """
    try:
        algorithm, iterations, salt, expected = stored_hash.split("$")
    except (AttributeError, ValueError):
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        )
    except ValueError:
        return False

    return hmac.compare_digest(digest.hex(), expected)


def create_access_token(username: str, role: str) -> str:
    """签发访问令牌

    Args:
        username: 用户名
        role: 角色（admin / operator）

    Returns:
        str: JWT 字符串
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解析并校验令牌

    Args:
        token: JWT 字符串

    Returns:
        dict: 载荷

    Raises:
        jwt.InvalidTokenError: 签名错误、过期等
    """
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[_ALGORITHM])


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict:
    """FastAPI 依赖：校验 Bearer Token

    Args:
        credentials: 由 HTTPBearer 解析出的凭证

    Returns:
        dict: {"username": str, "role": str}

    Raises:
        HTTPException: 401 未认证或 403 权限不足
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭证，请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username = payload.get("sub")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="凭证缺少用户标识",
        )

    return {"username": username, "role": payload.get("role", "operator")}


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI 依赖：要求管理员角色

    Args:
        user: 当前登录用户

    Returns:
        dict: 管理员用户信息

    Raises:
        HTTPException: 403 非管理员
    """
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该操作需要管理员权限",
        )
    return user
