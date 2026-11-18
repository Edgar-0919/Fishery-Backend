"""告警管理接口：查询活跃告警、查询已升级告警

告警状态存于 MySQL alert_duration 表，支持参数化查询与真正的 UPDATE。
路由使用同步函数，由 FastAPI 线程池执行，避免同步 DB 调用阻塞事件循环。
"""

import logging

from fastapi import APIRouter, Query

from app.core.db import execute_mysql

logger = logging.getLogger(__name__)

router = APIRouter()

_ALERT_COLUMNS = """
    id, pond_id, device_id, alert_type, alert_value, threshold_value,
    started_at, last_checked_at, duration_minutes, status,
    agent_decision, agent_action
"""


def _query_alerts_by_status(status: str, pond_id: str | None) -> dict:
    """按状态查询告警记录

    Args:
        status: alert_duration.status（active / escalated）
        pond_id: 可选的鱼塘筛选条件

    Returns:
        dict: 统一响应结构
    """
    try:
        if pond_id:
            rows = execute_mysql(
                f"SELECT {_ALERT_COLUMNS} FROM alert_duration "
                "WHERE status = %s AND pond_id = %s ORDER BY started_at DESC",
                (status, pond_id),
            )
        else:
            rows = execute_mysql(
                f"SELECT {_ALERT_COLUMNS} FROM alert_duration "
                "WHERE status = %s ORDER BY started_at DESC",
                (status,),
            )

        data_list = rows or []
        return {"success": True, "data": data_list, "count": len(data_list)}
    except Exception as e:
        logger.exception(f"查询告警失败 (status={status}): {e}")
        return {"success": False, "data": [], "message": str(e)}


@router.get("/active")
def get_active_alerts(
    pond_id: str = Query(None, description="鱼塘ID")
):
    """查询活跃告警"""
    return _query_alerts_by_status("active", pond_id)


@router.get("/escalated")
def get_escalated_alerts(
    pond_id: str = Query(None, description="鱼塘ID")
):
    """查询已升级告警"""
    return _query_alerts_by_status("escalated", pond_id)
