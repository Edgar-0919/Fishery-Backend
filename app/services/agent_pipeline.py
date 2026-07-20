"""Agent 决策链路：查询历史趋势、构建上下文、调用 LLM、执行指令、更新状态

核心流程（增强版）：
1. 查询同一鱼塘所有传感器的最新值（全局视图）
2. 查询设备当前状态（增氧机/水泵是否运行）
3. 查询最近 10 分钟该指标的历史数据（从 TDengine）
4. 构建完整决策上下文（全局传感器 + 设备状态 + 历史趋势）
5. 调用 LLM 进行智能决策（agent_decide）
6. 根据决策结果执行设备控制指令（executor，带安全校验）
7. 更新 alert_duration 表（新建或更新活跃告警记录）

调用时机：当 detector 检测到 critical 级别告警时，由 data_processor 异步触发
"""

import json
from datetime import datetime, timedelta

from app.core.db import execute_sql, execute_mysql
from app.agents.core import chat as agent_chat
from app.agents.decision import decide as agent_decide
from app.tools.executor import execute_with_check


async def agent_decision_pipeline(alert):
    """执行完整的 Agent 决策链路（增强版）

    增强点：
    1. 构建全局上下文：查询同一鱼塘所有传感器的最新值
    2. 设备状态感知：查询增氧机/水泵当前运行状态
    3. 多传感器关联分析：让 LLM 看到全局水质状况而非单一指标

    Args:
        alert: AlertEvent 对象，包含告警详情（pond_id, device_id, sensor_type, value, threshold, severity, trend, alert_type, message）

    Note:
        该函数作为后台异步任务执行，不阻塞主数据接收流程
        LLM 调用超时时间为 30 秒（在 agent/decision.py 中设置）
    """
    try:
        now = datetime.now()
        start = (now - timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
        end = now.strftime('%Y-%m-%d %H:%M:%S')

        all_sensors = await _get_all_sensors_latest(alert.pond_id, alert.device_id)
        device_status = await _get_device_status(alert.pond_id)

        sql = f"""
            SELECT ts, {alert.sensor_type} FROM fishery.sensor_data
            WHERE pond_id = '{alert.pond_id}' AND device_id = '{alert.device_id}'
            AND ts >= '{start}' AND ts <= '{end}'
            ORDER BY ts DESC
        """
        result = execute_sql(sql)
        history = []
        if result.get("code") == 0 and result.get("data"):
            for row in result["data"]:
                history.append({"ts": row[0], "value": row[1]})

        context = {
            "alert_type": alert.alert_type,
            "sensor_type": alert.sensor_type,
            "current_value": alert.value,
            "threshold": alert.threshold,
            "severity": alert.severity,
            "trend": alert.trend,
            "pond_id": alert.pond_id,
            "message": alert.message,
            "history_10min": history,
            "all_sensors": all_sensors,
            "device_status": device_status,
        }

        print(f"[AGENT] 开始决策: {alert.alert_type} pond={alert.pond_id}")
        print(f"[AGENT] 全局传感器: {json.dumps(all_sensors, ensure_ascii=False)}")
        print(f"[AGENT] 设备状态: {json.dumps(device_status, ensure_ascii=False)}")

        decision = agent_decide(context)
        analysis = decision.get("analysis", "")
        decision_body = decision.get("decision", {})
        action = decision_body.get("action", "no_action")
        reason = decision_body.get("reason", "")
        print(f"[AGENT] 决策结果: action={action} reason={reason}")

        if action != "no_action":
            execute_with_check(alert.pond_id, alert.device_id, decision_body, alert.alert_type, all_sensors)

        ts_str = now.strftime('%Y-%m-%d %H:%M:%S')
        safe_analysis = analysis.replace("'", "''")[:500]
        safe_action = action.replace("'", "''")[:100]

        check_sql = f"""
            SELECT ts, pond_id, device_id, alert_type, started_at
            FROM fishery.alert_duration
            WHERE pond_id = '{alert.pond_id}'
              AND device_id = '{alert.device_id}'
              AND alert_type = '{alert.alert_type}'
              AND status = 'active'
            ORDER BY ts DESC LIMIT 1
        """
        check_result = execute_sql(check_sql)
        has_active = (
            check_result.get("code") == 0
            and check_result.get("data")
            and len(check_result["data"]) > 0
        )

        if has_active:
            existing_ts = check_result["data"][0][0]
            existing_ts_str = str(existing_ts).replace("T", " ").replace(".000Z", "")
            delete_sql = f"DELETE FROM fishery.alert_duration WHERE ts = '{existing_ts_str}'"
            execute_sql(delete_sql)

            insert_sql = f"""
                INSERT INTO fishery.alert_duration
                (ts, pond_id, device_id, alert_type, alert_value, threshold_value,
                 started_at, last_checked_at, duration_minutes, status, agent_decision, agent_action)
                VALUES ('{existing_ts_str}', '{alert.pond_id}', '{alert.device_id}', '{alert.alert_type}',
                        {alert.value}, {alert.threshold}, '{ts_str}', '{ts_str}', 0, 'active',
                        '{safe_analysis}', '{safe_action}')
            """
            execute_sql(insert_sql)
            print(f"[AGENT] 告警已存在，更新决策记录: {alert.alert_type}")
        else:
            insert_sql = f"""
                INSERT INTO fishery.alert_duration
                (ts, pond_id, device_id, alert_type, alert_value, threshold_value,
                 started_at, last_checked_at, duration_minutes, status, agent_decision, agent_action)
                VALUES ('{ts_str}', '{alert.pond_id}', '{alert.device_id}', '{alert.alert_type}',
                        {alert.value}, {alert.threshold}, '{ts_str}', '{ts_str}', 0, 'active',
                        '{safe_analysis}', '{safe_action}')
            """
            execute_sql(insert_sql)

    except Exception as e:
        print(f"[AGENT] 决策链路异常: {e}")
        import traceback
        traceback.print_exc()


async def _get_all_sensors_latest(pond_id: str, device_id: str) -> dict:
    """获取同一鱼塘所有传感器的最新值

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID

    Returns:
        dict: 所有传感器的最新值，格式如 {"temperature": 25.5, "dissolved_oxygen": 6.2, ...}

    Note:
        查询最近 1 分钟内的最新记录，确保数据时效性
        返回所有 7 个指标（温度、pH、溶解氧、氨氮、亚硝酸盐、浊度、水位）
    """
    sql = f"""
        SELECT temperature, ph_value, dissolved_oxygen, ammonia_nitrogen,
               nitrite, ts_value, water_level
        FROM fishery.sensor_data
        WHERE pond_id = '{pond_id}' AND device_id = '{device_id}'
        ORDER BY ts DESC LIMIT 1
    """
    result = execute_sql(sql)
    if result.get("code") == 0 and result.get("data") and len(result["data"]) > 0:
        columns = ["temperature", "ph_value", "dissolved_oxygen", "ammonia_nitrogen",
                   "nitrite", "ts_value", "water_level"]
        row = result["data"][0]
        return dict(zip(columns, row))
    return {}


async def _get_device_status(pond_id: str) -> dict:
    """获取鱼塘设备的当前状态

    Args:
        pond_id: 鱼塘ID

    Returns:
        dict: 设备状态字典，格式如 {"aerator": "running", "pump": "stopped"}

    Note:
        从 MySQL 的 control_devices 表查询设备状态
        状态值：online（在线）、offline（离线）、running（运行中）
    """
    sql = "SELECT device_type, status FROM control_devices WHERE pond_id = %s"
    rows = execute_mysql(sql, (pond_id,))
    status = {}
    for row in rows:
        device_type = row.get("device_type", "")
        device_status = row.get("status", "")
        status[device_type] = device_status
    return status