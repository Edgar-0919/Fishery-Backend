"""告警管理接口：查询活跃告警、查询已升级告警"""

from fastapi import APIRouter, Query

from app.core.db import execute_sql

router = APIRouter()


@router.get("/active")
async def get_active_alerts(
    pond_id: str = Query(None, description="鱼塘ID")
):
    try:
        where = "WHERE status = 'active'"
        if pond_id:
            where += f" AND pond_id = '{pond_id}'"

        sql = f"""
            SELECT ts, pond_id, device_id, alert_type, alert_value,
                   threshold_value, started_at, last_checked_at,
                   duration_minutes, status, agent_decision, agent_action
            FROM fishery.alert_duration
            {where}
            ORDER BY started_at DESC
        """
        result = execute_sql(sql)

        data_list = []
        if result.get("code") == 0 and result.get("data"):
            columns = [col[0] for col in result.get("column_meta", [])]
            for row in result["data"]:
                data_list.append(dict(zip(columns, row)))

        return {"success": True, "data": data_list, "count": len(data_list)}
    except Exception as e:
        print(f"查询活跃告警失败: {e}")
        return {"success": False, "data": [], "message": str(e)}


@router.get("/escalated")
async def get_escalated_alerts(
    pond_id: str = Query(None, description="鱼塘ID")
):
    try:
        where = "WHERE status = 'escalated'"
        if pond_id:
            where += f" AND pond_id = '{pond_id}'"

        sql = f"""
            SELECT ts, pond_id, device_id, alert_type, alert_value,
                   threshold_value, started_at, last_checked_at,
                   duration_minutes, status, agent_decision, agent_action
            FROM fishery.alert_duration
            {where}
            ORDER BY started_at DESC
        """
        result = execute_sql(sql)

        data_list = []
        if result.get("code") == 0 and result.get("data"):
            columns = [col[0] for col in result.get("column_meta", [])]
            for row in result["data"]:
                data_list.append(dict(zip(columns, row)))

        return {"success": True, "data": data_list, "count": len(data_list)}
    except Exception as e:
        print(f"查询升级告警失败: {e}")
        return {"success": False, "data": [], "message": str(e)}