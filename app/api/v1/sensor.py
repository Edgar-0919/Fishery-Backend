"""传感器数据接口：实时数据查询、历史数据查询、模拟器数据接入

时序数据已合并到 MySQL，全部查询走 %s 参数化；动态指标列名经白名单校验。
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, BackgroundTasks

from app.core.db import execute_sql, safe_identifier
from app.models.schemas import SimulatorData
from app.api.v1.deps import get_data_processor

logger = logging.getLogger(__name__)

router = APIRouter()

_SENSOR_COLUMNS = [
    "ts", "pond_id", "device_id", "product_id",
    "temperature", "ph_value", "dissolved_oxygen",
    "ammonia_nitrogen", "nitrite", "ts_value", "water_level",
]

_ALLOWED_METRICS = {
    "temperature", "ph_value", "dissolved_oxygen", "ammonia_nitrogen",
    "nitrite", "ts_value", "water_level",
}


def _rows_to_dicts(result: dict, fallback_columns: list[str]) -> list[dict]:
    """把 execute_sql 的兼容返回结构转成 dict 列表

    column_meta 由 MySQL cursor.description 生成；测试 mock 可能不提供，
    此时用调用方显式给出的列顺序兜底。
    """
    if result.get("code") != 0 or not result.get("data"):
        return []
    meta = result.get("column_meta") or []
    columns = [col[0] for col in meta] or fallback_columns
    return [dict(zip(columns, row)) for row in result["data"]]


@router.get("")
def get_sensor_data(
    pond_id: str = Query(None, description="鱼塘ID"),
    device_id: str = Query(None, description="设备ID")
):
    try:
        now = datetime.now()
        end_time = now.strftime('%Y-%m-%d %H:%M:%S')
        start_time = (now.replace(second=0, microsecond=0) - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')

        conditions = ["ts >= %s", "ts <= %s"]
        params: list = [start_time, end_time]
        if pond_id:
            conditions.append("pond_id = %s")
            params.append(pond_id)
        if device_id:
            conditions.append("device_id = %s")
            params.append(device_id)

        sql = f"""
            SELECT {', '.join(_SENSOR_COLUMNS)}
            FROM fishery.sensor_data
            WHERE {' AND '.join(conditions)}
            ORDER BY ts DESC
        """
        result = execute_sql(sql, tuple(params))
        data_list = _rows_to_dicts(result, _SENSOR_COLUMNS)
        query_type = "recent_minute"

        if not data_list:
            # 最近一分钟无数据时回退到最近 10 条
            query_type = "latest_records"
            conditions, params = [], []
            if pond_id:
                conditions.append("pond_id = %s")
                params.append(pond_id)
            if device_id:
                conditions.append("device_id = %s")
                params.append(device_id)
            where_clause = " AND ".join(conditions) if conditions else "1=1"

            sql = f"""
                SELECT {', '.join(_SENSOR_COLUMNS)}
                FROM fishery.sensor_data
                WHERE {where_clause}
                ORDER BY ts DESC
                LIMIT 10
            """
            result = execute_sql(sql, tuple(params) or None)
            data_list = _rows_to_dicts(result, _SENSOR_COLUMNS)

        return {
            "success": True,
            "message": "查询成功",
            "data": data_list,
            "count": len(data_list),
            "query_type": query_type,
            "time_range": {
                "start": start_time,
                "end": end_time
            } if query_type == "recent_minute" else None
        }

    except Exception as e:
        logger.exception(f"查询传感器数据失败: {e}")
        return {
            "success": False,
            "message": f"查询失败: {str(e)}",
            "data": []
        }


@router.get("/history")
def get_sensor_data_history(
    pond_id: str = Query(..., description="鱼塘ID"),
    start: str = Query(..., description="开始时间"),
    end: str = Query(..., description="结束时间"),
    metrics: str = Query("temperature,ph_value,dissolved_oxygen", description="指标字段名")
):
    try:
        requested = [m.strip() for m in metrics.split(",") if m.strip() in _ALLOWED_METRICS]
        if not requested:
            return {"success": False, "message": "无效的指标参数"}

        columns = ", ".join(safe_identifier(m) for m in requested)

        sql = f"""
            SELECT ts, {columns}
            FROM fishery.sensor_data
            WHERE pond_id = %s
              AND ts >= %s
              AND ts <= %s
            ORDER BY ts ASC
            LIMIT 500
        """
        result = execute_sql(sql, (pond_id, start, end))
        data_list = _rows_to_dicts(result, ["ts", *requested])

        return {"success": True, "data": data_list, "count": len(data_list)}
    except Exception as e:
        logger.exception(f"查询历史数据失败: {e}")
        return {"success": False, "data": [], "message": str(e)}


@router.post("/simulator/data")
def simulator_data_endpoint(req: SimulatorData, background_tasks: BackgroundTasks):
    params = {}
    if req.temperature is not None:
        params['temp'] = {'value': req.temperature, 'time': int(datetime.now().timestamp() * 1000)}
    if req.ph_value is not None:
        params['pH'] = {'value': req.ph_value, 'time': int(datetime.now().timestamp() * 1000)}
    if req.dissolved_oxygen is not None:
        params['DO'] = {'value': req.dissolved_oxygen, 'time': int(datetime.now().timestamp() * 1000)}
    if req.ammonia_nitrogen is not None:
        params['NH3'] = {'value': req.ammonia_nitrogen, 'time': int(datetime.now().timestamp() * 1000)}
    if req.nitrite is not None:
        params['NO2'] = {'value': req.nitrite, 'time': int(datetime.now().timestamp() * 1000)}
    if req.ts_value is not None:
        params['TS'] = {'value': req.ts_value, 'time': int(datetime.now().timestamp() * 1000)}
    if req.water_level is not None:
        params['WL'] = {'value': req.water_level, 'time': int(datetime.now().timestamp() * 1000)}

    data = {
        'deviceName': req.device_id,
        'productId': req.product_id,
        'data': {'params': params}
    }

    data_processor = get_data_processor()
    background_tasks.add_task(data_processor.process_data_point, data)
    return {"status": "ok", "message": "数据已接收，后台处理中"}
