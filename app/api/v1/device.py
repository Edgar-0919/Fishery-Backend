"""设备控制接口：查询设备列表、手动下发控制指令

路由使用同步函数，由 FastAPI 线程池执行，避免同步 DB 调用阻塞事件循环。

人工指令同样经过安全规则校验：
- 前端已完成二次确认的动作，视为 human_confirmed，放行「需人工确认」的高风险动作
- 但「溶解氧过低禁止关闭增氧机」这类硬性安全规则仍然拦截，人工也不能绕过
"""

import logging

from fastapi import APIRouter, Query

from app.core.db import execute_mysql
from app.models.schemas import DeviceCommandRequest
from app.tools.executor import execute_with_check

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
def get_control_devices(pond_id: str = Query(None, description="鱼塘ID")):
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
        logger.exception(f"查询设备列表失败: {e}")
        return {"success": False, "data": [], "message": str(e)}


@router.post("/{device_identifier}/command")
def send_device_command(device_identifier: str, req: DeviceCommandRequest):
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

        # 人工指令走统一执行器：安全规则、指令记录、设备状态同步一次完成。
        # sensor_device_id 传空串，因为控制设备的标识（aerator_pond1）与
        # sensor_data 表中的采集设备ID（sim_dev_001）不同源，需要按鱼塘取最新数据。
        result = execute_with_check(
            pond_id=pond_id,
            device_id=device_identifier,
            decision={
                "action": req.command_type,
                "params": req.params,
                "reason": req.reason or "人工下发控制指令",
            },
            alert_type="manual_command",
            sensor_device_id="",
            skip_cooldown=True,
            human_confirmed=True,
            trigger_source="manual",
        )

        status = result.get("status")
        logger.warning(
            f"人工下发指令: {req.command_type} | device={device_identifier} "
            f"| pond={pond_id} | params={req.params} | 结果={status}"
        )

        if status == "executed":
            return {
                "success": True,
                "message": f"指令 {req.command_type} 已下发至 {device_identifier}",
            }
        if status == "skipped":
            # 设备已处于目标状态，对操作员来说是成功语义
            return {
                "success": True,
                "message": result.get("reason") or "设备已处于目标状态，无需重复下发",
            }
        if status == "blocked":
            return {
                "success": False,
                "message": f"指令被安全规则拦截：{result.get('reason') or '不满足执行条件'}",
            }
        return {"success": False, "message": result.get("reason") or "指令未能执行"}
    except Exception as e:
        logger.exception(f"下发设备指令失败: {e}")
        return {"success": False, "message": str(e)}
