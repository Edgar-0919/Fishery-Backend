"""指令执行器（增强版）：解析 Agent 决策，执行设备控制指令，记录执行结果

安全增强功能：
1. 安全规则校验：执行前检查安全规则，拦截危险指令
2. 设备状态检查：避免重复下发已执行的指令
3. 冷却期机制：同一设备 5 分钟内不重复下发相同指令
4. 高风险动作保护：stop_aerator/stop_pump 默认需人工确认
5. 效果跟踪：执行后 15 分钟检查指标是否改善，未改善自动升级

配置参数（来自 settings）：
- COOLDOWN_MINUTES: 冷却期（默认 5 分钟）
- EFFECT_CHECK_MINUTES: 效果检查间隔（默认 15 分钟）
- HIGH_RISK_ACTIONS: 需要人工确认的高风险动作列表
"""

import json
import asyncio
from datetime import datetime
from typing import Dict, Optional

from app.core.db import execute_sql, execute_mysql
from app.core.config import settings
from app.tools.rules import check_safety_rules, check_effect_tracking_rule, is_high_risk_action

_last_command_time: Dict[str, datetime] = {}


def execute(pond_id: str, device_id: str, decision: dict, alert_type: str) -> dict:
    """直接执行指令（绕过安全校验）

    适用于紧急规则触发的场景，不经过安全校验直接执行。
    正常决策流程应使用 execute_with_check()。

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        decision: 决策字典，包含 action、params、reason
        alert_type: 告警类型

    Returns:
        dict: 执行结果
    """
    action = decision.get("action", "no_action")
    params = decision.get("params", {})
    reason = decision.get("reason", "")
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[EXECUTOR] 执行指令: {action} | pond={pond_id} | params={params}")
    print(f"[EXECUTOR] 理由: {reason}")

    params_json = json.dumps(params, ensure_ascii=False)
    safe_reason = reason.replace("'", "''")[:500]
    safe_params = params_json.replace("'", "''")[:500]

    sql = f"""
        INSERT INTO fishery.device_commands
        (ts, pond_id, device_id, command_type, trigger_source, trigger_alert_id,
         params, status, executed_at, result)
        VALUES ('{ts_str}', '{pond_id}', '{device_id}', '{action}', 'agent', '{alert_type}',
                '{safe_params}', 'executed', '{ts_str}', '模拟执行成功')
    """
    execute_sql(sql)

    _update_cooldown(pond_id, device_id, action)

    return {"action": action, "status": "executed", "reason": reason}


def execute_with_check(pond_id: str, device_id: str, decision: dict,
                       alert_type: str, sensor_data: Optional[Dict[str, float]] = None) -> dict:
    """带安全校验的指令执行（推荐使用）

    执行流程：
    1. 冷却期检查：同一设备同一动作 5 分钟内不重复执行
    2. 设备状态检查：查询当前设备状态，避免重复指令
    3. 安全规则校验：调用 check_safety_rules 拦截危险指令
    4. 高风险动作判断：stop_aerator/stop_pump 默认进入待确认状态
    5. 执行指令并记录
    6. 设置效果跟踪任务（15 分钟后检查）

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        decision: 决策字典，包含 action、params、reason
        alert_type: 告警类型
        sensor_data: 当前传感器数据（用于安全校验）

    Returns:
        dict: 执行结果，包含 action、status、reason、blocked_by（若被拦截）
    """
    action = decision.get("action", "no_action")
    params = decision.get("params", {})
    reason = decision.get("reason", "")

    if action == "no_action":
        return {"action": action, "status": "skipped", "reason": "无需执行"}

    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not _check_cooldown(pond_id, device_id, action):
        print(f"[EXECUTOR] 冷却期未过，跳过指令: {action}")
        return {
            "action": action,
            "status": "blocked",
            "blocked_by": "Cooldown",
            "reason": f"同一设备同一动作冷却期未过（{settings.COOLDOWN_MINUTES}分钟）"
        }

    current_status = _get_device_current_status(pond_id, action)
    if current_status == "running":
        if action.startswith("start_"):
            print(f"[EXECUTOR] 设备已在运行，跳过启动指令: {action}")
            return {
                "action": action,
                "status": "skipped",
                "reason": "设备已在运行"
            }

    if sensor_data is None:
        sensor_data = _get_latest_sensors(pond_id, device_id)

    safety_result = check_safety_rules(pond_id, device_id, action, sensor_data)
    if not safety_result.allowed:
        if safety_result.requires_approval:
            print(f"[EXECUTOR] 高风险动作，进入待确认状态: {action}")
            _save_pending_command(pond_id, device_id, action, params, reason, alert_type)
            return {
                "action": action,
                "status": "pending_approval",
                "reason": safety_result.reason
            }
        else:
            print(f"[EXECUTOR] 安全规则拦截指令: {action} | {safety_result.reason}")
            return {
                "action": action,
                "status": "blocked",
                "blocked_by": safety_result.blocked_by,
                "reason": safety_result.reason
            }

    print(f"[EXECUTOR] 执行指令: {action} | pond={pond_id} | params={params}")
    print(f"[EXECUTOR] 理由: {reason}")

    params_json = json.dumps(params, ensure_ascii=False)
    safe_reason = reason.replace("'", "''")[:500]
    safe_params = params_json.replace("'", "''")[:500]

    sql = f"""
        INSERT INTO fishery.device_commands
        (ts, pond_id, device_id, command_type, trigger_source, trigger_alert_id,
         params, status, executed_at, result)
        VALUES ('{ts_str}', '{pond_id}', '{device_id}', '{action}', 'agent', '{alert_type}',
                '{safe_params}', 'executed', '{ts_str}', '模拟执行成功')
    """
    execute_sql(sql)

    _update_cooldown(pond_id, device_id, action)

    if action in ("start_aerator", "start_pump"):
        asyncio.create_task(_schedule_effect_check(
            pond_id, device_id, action, sensor_data, ts_str
        ))

    return {"action": action, "status": "executed", "reason": reason}


def _check_cooldown(pond_id: str, device_id: str, action: str) -> bool:
    """检查冷却期

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        action: 动作类型

    Returns:
        bool: True=冷却期已过，可以执行；False=冷却期未过，跳过执行
    """
    key = f"{pond_id}:{device_id}:{action}"
    last_time = _last_command_time.get(key)
    if last_time is None:
        return True
    elapsed_minutes = (datetime.now() - last_time).total_seconds() / 60
    return elapsed_minutes >= settings.COOLDOWN_MINUTES


def _update_cooldown(pond_id: str, device_id: str, action: str):
    """更新冷却期时间戳

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        action: 动作类型
    """
    key = f"{pond_id}:{device_id}:{action}"
    _last_command_time[key] = datetime.now()


def _get_device_current_status(pond_id: str, action: str) -> str:
    """获取设备当前状态

    Args:
        pond_id: 鱼塘ID
        action: 动作类型（用于推断设备类型）

    Returns:
        str: "running"（运行中）、"stopped"（已停止）或 ""（未知）
    """
    device_type = ""
    if "aerator" in action:
        device_type = "aerator"
    elif "pump" in action:
        device_type = "pump"
    elif "feeder" in action:
        device_type = "feeder"

    if not device_type:
        return ""

    sql = "SELECT status FROM control_devices WHERE pond_id = %s AND device_type = %s"
    rows = execute_mysql(sql, (pond_id, device_type))
    if rows and len(rows) > 0:
        return rows[0].get("status", "")
    return ""


def _get_latest_sensors(pond_id: str, device_id: str) -> Dict[str, float]:
    """获取最新传感器数据（用于安全校验）

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID

    Returns:
        dict: 所有传感器的最新值
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


def _save_pending_command(pond_id: str, device_id: str, action: str,
                          params: dict, reason: str, alert_type: str):
    """保存待确认的指令

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        action: 动作类型
        params: 动作参数
        reason: 执行理由
        alert_type: 关联的告警类型
    """
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params_json = json.dumps(params, ensure_ascii=False)
    safe_reason = reason.replace("'", "''")[:500]
    safe_params = params_json.replace("'", "''")[:500]

    sql = f"""
        INSERT INTO fishery.device_commands
        (ts, pond_id, device_id, command_type, trigger_source, trigger_alert_id,
         params, status, executed_at, result)
        VALUES ('{ts_str}', '{pond_id}', '{device_id}', '{action}', 'agent', '{alert_type}',
                '{safe_params}', 'pending_approval', NULL, '待人工确认')
    """
    execute_sql(sql)


async def _schedule_effect_check(pond_id: str, device_id: str, action: str,
                                 sensor_data_before: Dict[str, float], executed_at: str):
    """调度效果检查任务

    在指令执行后 EFFECT_CHECK_MINUTES（默认15分钟）检查指标是否改善，
    未改善时自动升级告警级别。

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        action: 执行的动作
        sensor_data_before: 执行前的传感器数据
        executed_at: 执行时间
    """
    await asyncio.sleep(settings.EFFECT_CHECK_MINUTES * 60)

    sensor_data_after = _get_latest_sensors(pond_id, device_id)
    if not sensor_data_after:
        print(f"[EXECUTOR] 效果检查：无法获取最新传感器数据")
        return

    effect_ok = check_effect_tracking_rule(pond_id, device_id, action,
                                           sensor_data_before, sensor_data_after)

    if not effect_ok:
        print(f"[EXECUTOR] 效果检查失败：{action} 执行后指标未改善")
        print(f"[EXECUTOR] 执行前: {sensor_data_before}")
        print(f"[EXECUTOR] 执行后: {sensor_data_after}")
        _escalate_alert(pond_id, device_id, action, sensor_data_before, sensor_data_after)


def _escalate_alert(pond_id: str, device_id: str, action: str,
                    sensor_data_before: Dict[str, float],
                    sensor_data_after: Dict[str, float]):
    """升级告警级别

    当指令执行效果未达到预期时，创建升级告警并报人工处理。

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        action: 执行的动作
        sensor_data_before: 执行前的传感器数据
        sensor_data_after: 执行后的传感器数据
    """
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    before_str = json.dumps(sensor_data_before, ensure_ascii=False)[:200]
    after_str = json.dumps(sensor_data_after, ensure_ascii=False)[:200]

    sql = f"""
        INSERT INTO fishery.alerts
        (ts, pond_id, device_id, alert_type, alert_value, threshold_value, message, is_read)
        VALUES ('{ts_str}', '{pond_id}', '{device_id}', 'effect_check_failed', 0, 0,
                '指令执行效果未达标: {action} | 执行前: {before_str} | 执行后: {after_str}', false)
    """
    execute_sql(sql)

    from app.tools.alert_tracker import _create_manual_task
    _create_manual_task(pond_id, "effect_check_failed", {
        "alert_value": 0,
        "threshold_value": 0,
        "started_at": ts_str
    }, datetime.now())

    print(f"[EXECUTOR] 告警已升级并创建人工工单: pond={pond_id}")