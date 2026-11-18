"""用户数据访问：登录校验、默认管理员初始化、密码修改

用户表存于 MySQL，密码以 pbkdf2 哈希存储，任何地方都不保存明文。
"""

import logging

from app.core.config import settings
from app.core.db import execute_mysql
from app.core.security import hash_password, verify_password

logger = logging.getLogger(__name__)


def get_user(username: str) -> dict | None:
    """按用户名查询用户

    Args:
        username: 用户名

    Returns:
        dict | None: 用户记录（含 password_hash），不存在返回 None
    """
    rows = execute_mysql(
        """
        SELECT id, username, password_hash, display_name, role, is_active
        FROM users
        WHERE username = %s
        LIMIT 1
        """,
        (username,),
    )
    return rows[0] if rows else None


def authenticate(username: str, password: str) -> dict | None:
    """校验用户名密码

    Args:
        username: 用户名
        password: 明文密码

    Returns:
        dict | None: 校验通过返回用户公开信息，失败返回 None
    """
    user = get_user(username)
    if not user:
        return None
    if not user.get("is_active"):
        return None
    if not verify_password(password, user.get("password_hash", "")):
        return None

    return {
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "role": user.get("role") or "operator",
    }


def update_password(username: str, new_password: str) -> bool:
    """更新用户密码

    Args:
        username: 用户名
        new_password: 新明文密码

    Returns:
        bool: 是否更新成功
    """
    execute_mysql(
        "UPDATE users SET password_hash = %s WHERE username = %s",
        (hash_password(new_password), username),
    )
    # execute_mysql 写操作返回 None，这里用重新查询确认结果
    user = get_user(username)
    return bool(user and verify_password(new_password, user.get("password_hash", "")))


def ensure_default_admin() -> None:
    """确保存在可登录的默认管理员

    仅在 users 表为空时创建，已存在任何账号则不覆盖。
    默认账号密码来自 settings，生产部署应通过 .env 修改。
    """
    rows = execute_mysql("SELECT COUNT(*) AS total FROM users")
    if rows is None:
        # 数据库不可用或表不存在，跳过初始化（由 /health/ready 暴露问题）
        logger.warning("users 表不可用，跳过默认管理员初始化")
        return

    total = rows[0].get("total", 0) if rows else 0
    if total:
        return

    execute_mysql(
        """
        INSERT INTO users (username, password_hash, display_name, role, is_active)
        VALUES (%s, %s, %s, 'admin', 1)
        """,
        (
            settings.DEFAULT_ADMIN_USERNAME,
            hash_password(settings.DEFAULT_ADMIN_PASSWORD),
            "系统管理员",
        ),
    )
    logger.warning(
        f"已创建默认管理员: {settings.DEFAULT_ADMIN_USERNAME}（请尽快修改密码）"
    )
