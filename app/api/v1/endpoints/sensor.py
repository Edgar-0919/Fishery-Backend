"""传感器数据接口：实时数据查询、历史数据查询、模拟器数据接入"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Query, BackgroundTasks

from app.core.db import execute_sql
from app.models.schemas import SimulatorData
from app.api.v1.deps import get_data_processor

router = APIRouter()


@router.get("")
async def get_sensor_data(
    pond_id: str = Query(None, description="鱼塘ID"),
    device_id: str = Query(None, description="设备ID")
):
    try:
        now = datetime.now()
        end_time = now.strftime('%Y-%m-%d %H:%M:%S')
        start_time = (now.replace(second=0, microsecond=0) - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')

        where_conditions = [f"ts >= '{start_time}'", f"ts <= '{end_time}'"]

        if pond_id:
            where_conditions.append(f"pond_id = '{pond_id}'")

        if device_id:
            where_conditions.append(f"device_id = '{device_id}'")

        where_clause = " AND ".join(where_conditions)

        sql = f"""
            SELECT ts, pond_id, device_id, product_id,
                   temperature, ph_value, dissolved_oxygen, ammonia_nitrogen, nitrite, ts_value, water_level
            FROM fishery.sensor_data
            WHERE {where_clause}
            ORDER BY ts DESC
        """

        result = execute_sql(sql)

        data_list = []
        query_type = "recent_minute"

        if result.get('code') == 0 and result.get('data') and len(result['data']) > 0:
            columns = [col[0] for col in result.get('column_meta', [])]
            for row in result['data']:
                data_dict = dict(zip(columns, row))
                data_list.append(data_dict)
        else:
            query_type = "latest_records"

            where_conditions = []
            if pond_id:
                where_conditions.append(f"pond_id = '{pond_id}'")

            if device_id:
                where_conditions.append(f"device_id = '{device_id}'")

            where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"

            sql = f"""
                SELECT ts, pond_id, device_id, product_id,
                       temperature, ph_value, dissolved_oxygen, ammonia_nitrogen, nitrite, ts_value, water_level
                FROM fishery.sensor_data
                WHERE {where_clause}
                ORDER BY ts DESC
                LIMIT 10
            """

            result = execute_sql(sql)

            if result.get('code') == 0 and result.get('data'):
                columns = [col[0] for col in result.get('column_meta', [])]
                for row in result['data']:
                    data_dict = dict(zip(columns, row))
                    data_list.append(data_dict)

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
        print(f"查询传感器数据失败: {e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "message": f"查询失败: {str(e)}",
            "data": []
        }


@router.get("/history")
async def get_sensor_data_history(
    pond_id: str = Query(..., description="鱼塘ID"),
    start: str = Query(..., description="开始时间"),
    end: str = Query(..., description="结束时间"),
    metrics: str = Query("temperature,ph_value,dissolved_oxygen", description="指标字段名")
):
    try:
        allowed = {"temperature", "ph_value", "dissolved_oxygen", "ammonia_nitrogen",
                   "nitrite", "ts_value", "water_level"}
        requested = [m.strip() for m in metrics.split(",") if m.strip() in allowed]
        if not requested:
            return {"success": False, "message": "无效的指标参数"}

        columns = ", ".join(requested)

        sql = f"""
            SELECT ts, {columns}
            FROM fishery.sensor_data
            WHERE pond_id = '{pond_id}'
              AND ts >= '{start}'
              AND ts <= '{end}'
            ORDER BY ts ASC
            LIMIT 500
        """
        result = execute_sql(sql)

        data_list = []
        if result.get("code") == 0 and result.get("data"):
            meta_columns = [col[0] for col in result.get("column_meta", [])]
            for row in result["data"]:
                data_list.append(dict(zip(meta_columns, row)))

        return {"success": True, "data": data_list, "count": len(data_list)}
    except Exception as e:
        print(f"查询历史数据失败: {e}")
        return {"success": False, "data": [], "message": str(e)}


@router.post("/simulator/data")
async def simulator_data_endpoint(req: SimulatorData, background_tasks: BackgroundTasks):
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