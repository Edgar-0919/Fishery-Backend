"""数据库操作：统一 MySQL 连接池（业务数据 + 时序数据）

设计要点：
1. 业务表与 sensor_data / device_status 时序表都存 MySQL，共用连接池
2. execute_sql() 是时序查询适配层，保留旧 TDengine REST 的返回结构
   {"code":0, "column_meta":[...], "data":[...]}，使历史调用方无需改动
3. 外部输入一律走 %s 参数化；动态列名/表名用 safe_identifier() 白名单化
"""

import logging
import re
import threading

import pymysql
from dbutils.pooled_db import PooledDB

from app.core.config import settings

logger = logging.getLogger(__name__)

# MySQL 连接池配置
_POOL_MIN_CACHED = 2
_POOL_MAX_CACHED = 5
_POOL_MAX_CONNECTIONS = 10

_pool: PooledDB | None = None
_pool_lock = threading.Lock()


def escape_sql_string(val) -> str:
    """SQL 字符串转义：将单引号加倍（兼容保留）

    外部输入现已统一使用 %s 参数化查询，本函数仅用于少数必须拼接字符串
    的场景，不应作为防注入的主要手段。

    Args:
        val: 待转义的值，支持 str、int、float、None

    Returns:
        str: 转义后的字符串，None 返回 'NULL'
    """
    if val is None:
        return "NULL"
    if isinstance(val, (int, float)):
        return str(val)
    return str(val).replace("\\", "\\\\").replace("'", "''")


def safe_identifier(value, max_length: int = 50) -> str:
    """把业务标识转换为合法的 SQL 标识符片段

    仅允许字母、数字、下划线，用于动态列名/表名白名单化；
    鱼塘/设备 ID 中的连字符等字符会被替换。

    Args:
        value: 原始标识（如 sensor_type、pond_id）
        max_length: 截断长度

    Returns:
        str: 仅含 [0-9A-Za-z_] 的标识符片段
    """
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", str(value or ""))
    return (cleaned or "unknown")[:max_length]


def _get_pool() -> PooledDB:
    """获取 MySQL 连接池单例（首次调用时创建）"""
    global _pool
    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is None:
            _pool = PooledDB(
                creator=pymysql,
                mincached=_POOL_MIN_CACHED,
                maxcached=_POOL_MAX_CACHED,
                maxconnections=_POOL_MAX_CONNECTIONS,
                blocking=True,
                ping=1,
                host=settings.MYSQL_HOST,
                port=settings.MYSQL_PORT,
                user=settings.MYSQL_USER,
                password=settings.MYSQL_PASSWORD,
                database=settings.MYSQL_DATABASE,
                charset="utf8mb4",
            )
    return _pool


def execute_mysql(sql: str, params=None) -> list[dict] | None:
    """执行 SQL 语句到 MySQL，返回 list[dict]

    Args:
        sql: 参数化 SQL（使用 %s 占位符，禁止拼接用户输入）
        params: SQL 参数元组/列表，无参数时传 None

    Returns:
        list[dict]: 查询结果；写操作成功返回 None；失败返回 None
    """
    try:
        conn = _get_pool().connection()
    except Exception as exc:
        logger.error(f"MySQL 获取连接失败: {exc}")
        return None

    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(sql, params)
            if cursor.description:
                return cursor.fetchall()
            conn.commit()
            return None
    except Exception as exc:
        logger.error(f"MySQL 执行失败: {exc} | SQL: {sql[:500]}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        conn.close()


def execute_mysql_transaction(statements: list[tuple[str, tuple | list | None]]) -> bool:
    """在同一连接/事务内执行多条写 SQL，全部成功才提交

    用于需要原子性的场景（例如一轮问答要同时写会话与两条消息），
    避免「user 已落库、assistant 写失败」产生孤儿记录。

    Args:
        statements: [(sql, params), ...]，params 可为 None

    Returns:
        bool: 是否全部执行并提交成功
    """
    try:
        conn = _get_pool().connection()
    except Exception as exc:
        logger.error(f"MySQL 获取连接失败: {exc}")
        return False

    try:
        conn.begin()
        with conn.cursor() as cursor:
            for sql, params in statements:
                cursor.execute(sql, params)
        conn.commit()
        return True
    except Exception as exc:
        logger.error(f"MySQL 事务执行失败: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def execute_sql(sql: str, params=None, timeout=None) -> dict:
    """执行时序数据 SQL，返回兼容旧 TDengine REST 的结构

    迁移背景：sensor_data / device_status 原存 TDengine，经 HTTP REST 查询。
    合并到 MySQL 后保留 {"code":0, "column_meta":[[列名,None],...],
    "data":[[值,...],...]} 的返回结构，让所有历史调用方与测试 mock 无需改动，
    同时通过 params 支持 %s 参数化查询。

    Args:
        sql: SQL 语句，推荐 %s 占位符
        params: 参数元组/列表
        timeout: 兼容旧调用保留；MySQL 连接池自带超时，不使用

    Returns:
        dict: 成功 {"code":0,"column_meta":[...],"data":[...]}；
              失败 {"code":-1,"desc":...,"data":None}
    """
    conn = None
    try:
        conn = _get_pool().connection()
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            if cursor.description:
                rows = cursor.fetchall()
                column_meta = [[desc[0], None] for desc in cursor.description]
                data = [list(row) for row in rows]
            else:
                column_meta, data = [], []
            conn.commit()
            return {"code": 0, "column_meta": column_meta, "data": data}
    except Exception as exc:
        logger.error("时序数据 SQL 执行失败: %s | SQL: %s", exc, sql[:500])
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return {"code": -1, "desc": f"时序数据查询失败: {exc}", "data": None}
    finally:
        if conn is not None:
            conn.close()
