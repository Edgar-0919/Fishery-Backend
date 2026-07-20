"""设备控制接口：查询设备列表、手动下发控制指令"""

import json
from datetime import datetime

from fastapi import APIRouter, Query

from app.core.db import execute_mysql, execute_sql
from app.models.schemas import DeviceCommandRequest

router = APIRouter()


@router.get("")
async def get_control_devices(pond_id: str = Query(None, description="鱼塘ID")):
    try:
        conditions = []
        params = []
        if pond_id:
            conditions.append("pond_id = %s")
            params.append(pond_id)
        where = " AND ".join(conditions) if conditions else "1=1"

        sql = f"""
            SELECT id, pond_id, device_type, device_name, device_identifier, status, updated_at
            FROM control_devices
            WHERE {where}
            ORDER BY pond_id, device_type
        """
        rows = execute_mysql(sql, tuple(params) if params else None)
        return {"success": True, "data": rows or [], "count": len(rows) if rows else 0}
    except Exception as e:
        print(f"查询设备列表失败: {e}")
        return {"success": False, "data": [], "message": str(e)}


@router.post("/{device_identifier}/command")
async def send_device_command(device_identifier: str, req: DeviceCommandRequest):
    try:
        sql = """
            SELECT pond_id, device_name, device_type FROM control_devices
            WHERE device_identifier = %s
        """
        rows = execute_mysql(sql, (device_identifier,))
        if not rows:
            return {"success": False, "message": f"设备 {device_identifier} 不存在"}

        device = rows[0]
        pond_id = device["pond_id"]
        ts_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        params_json = json.dumps(req.params, ensure_ascii=False)
        safe_params = params_json.replace("'", "''")[:500]

        insert_sql = f"""
            INSERT INTO fishery.device_commands
            (ts, pond_id, device_id, command_type, trigger_source, trigger_alert_id,
             params, status, executed_at, result)
            VALUES ('{ts_str}', '{pond_id}', '{device_identifier}', '{req.command_type}',
                    'manual', '', '{safe_params}', 'executed', '{ts_str}', '手动控制执行成功')
        """
        execute_sql(insert_sql)

        print(f"[DEVICE] 手动控制: {req.command_type} | device={device_identifier} | params={req.params}")
        return {"success": True, "message": f"指令 {req.command_type} 已下发至 {device_identifier}"}
    except Exception as e:
        print(f"下发设备指令失败: {e}")
        return {"success": False, "message": str(e)}