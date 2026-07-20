"""数据库操作：TDengine SQL 执行 + MySQL SQL 执行"""

import pymysql
import requests

from app.core.config import settings


def execute_sql(sql: str) -> dict:
    """执行 SQL 语句到 TDengine，返回 JSON 结果"""
    headers = {"Authorization": settings.TDENGINE_AUTH}
    response = requests.post(settings.TDENGINE_URL, data=sql, headers=headers)
    result = response.json()
    if result.get("code") != 0:
        print(f"[DB] SQL 执行失败: {result.get('desc')}")
        print(f"[DB] SQL: {sql}")
    return result


def execute_mysql(sql: str, params=None) -> list[dict] | None:
    """执行 SQL 语句到 MySQL，返回 list[dict] 或 None"""
    try:
        conn = pymysql.connect(
            host=settings.MYSQL_HOST,
            port=settings.MYSQL_PORT,
            user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD,
            database=settings.MYSQL_DATABASE,
            charset="utf8mb4",
        )
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(sql, params)
            if cursor.description:
                rows = cursor.fetchall()
                return rows
            else:
                conn.commit()
                return None
    except Exception as e:
        print(f"[MySQL] 执行失败: {e}")
        print(f"[MySQL] SQL: {sql}")
        return None
    finally:
        try:
            conn.close()
        except:
            pass