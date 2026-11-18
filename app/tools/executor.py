"""指令执行器（增强版）：解析 Agent 决策，执行设备控制指令，记录执行结果

安全增强功能：
1. 安全规则校验：执行前检查安全规则，拦截危险指令
2. 设备状态检查：避免重复下发已执行的指令
3. 冷却期机制：同一设备 5 分钟内不重复下发相同指令（依据落库的指令记录判断）
4. 高风险动作保护：stop_aerator/stop_pump 默认需人工确认
5. 效果跟踪：执行后 EFFECT_CHECK_MINUTES（默认 15 分钟）检查指标是否改善，
   未改善自动升级为人工工单

冷却期为什么落库：
    原实现用进程内字典 _last_command_time 记录时间戳，服务重启即清零，
    多实例部署时各进程也互不可见。改为查询 device_commands 中最近一条
    同类已执行指令，天然持久化且与指令记录同源。

效果跟踪的调度方式：
    不在进程内用 asyncio.sleep 挂 15 分钟（重启即丢、且在同步上下文无法创建任务），
    而是在 device_commands 表标记 effect_checked=0，由 alert_tracker 的调度器
    定时扫描到期记录。状态落库后进程重启不会丢失检查任务。

配置参数（来自 settings）：
- COOLDOWN_MINUTES: 冷却期（默认 5 分钟）
- EFFECT_CHECK_MINUTES: 效果检查间隔（默认 15 分钟）
- HIGH_RISK_ACTIONS: 需要人工确认的高风险动作列表

闭环边界（硬件未接线期）：
    决策链路的终点是把动作登记进 ``device_commands``。本模块不调用
    ``app.agent.gateway.DeviceGateway.dispatch``，也不宣称硬件已执行；
    ``result`` 统一写入 ``_UNWIRED_RESULT`` 显式说明未接线。
    接真实硬件时按 gateway.py 模块注释的三步替换该文案并写入设备回执。
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from app.core.config import settings
from app.core.db import execute_mysql, execute_sql
from app.tools.rules import check_effect_tracking_rule, check_safety_rules

logger = logging.getLogger(__name__)

# 硬件未接线时写入 device_commands.result 的统一文案。
# 不得写成「执行成功」：没有设备回执，就没有执行结果。
_UNWIRED_RESULT = "[STUB] 指令已登记，DeviceGateway.dispatch 未接线（硬件未接入）"

# 需要做效果跟踪的动作（启动设备后验证指标是否改善）
_EFFECT_TRACKED_ACTIONS = ("start_aerator", "start_pump")


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
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    logger.warning(f"紧急指令直接执行: {action} | pond={pond_id} | params={params} | 理由={reason}")

    _insert_command(
        pond_id=pond_id,
        device_id=device_id,
        action=action,
        params=params,
        reason=reason,
        alert_type=alert_type,
        trigger_source="agent",
        status="executed",
        result=_UNWIRED_RESULT,
        effect_tracked=False,
        sensor_snapshot=None,
        ts_str=ts_str,
    )

    _sync_device_status(pond_id, action)

    return {"action": action, "status": "executed", "reason": reason}


def execute_with_check(
    pond_id: str,
    device_id: str,
    decision: dict,
    alert_type: str,
    sensor_data: Optional[Dict[str, float]] = None,
    *,
    sensor_device_id: Optional[str] = None,
    skip_cooldown: bool = False,
    human_confirmed: bool = False,
    trigger_source: str = "agent",
) -> dict:
    """带安全校验的指令执行（推荐使用）

    执行流程：
    1. 冷却期检查：同一设备同一动作 5 分钟内不重复执行
    2. 设备状态检查：查询当前设备状态，避免重复指令
    3. 安全规则校验：调用 check_safety_rules 拦截危险指令
    4. 高风险动作判断：stop_aerator/stop_pump 默认进入待确认状态
    5. 执行指令并记录，需要跟踪的动作写入 effect_checked=0 待后续验证

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID / 设备标识（写入指令记录，并作为冷却期键）
        decision: 决策字典，包含 action、params、reason
        alert_type: 告警类型
        sensor_data: 当前传感器数据（用于安全校验与效果比对）
        sensor_device_id: 查询传感器用的设备ID。控制类设备的标识（如 aerator_pond1）
            与 sensor_data 表中的采集设备ID（如 sim_dev_001）不是同一个值，
            这里允许单独指定；传空字符串表示按鱼塘查询任意设备的最新数据
        skip_cooldown: 跳过冷却期检查。人工下发指令时使用，
            避免操作员在紧急情况下被 5 分钟冷却期拦住
        human_confirmed: 人工已确认。为 True 时放行「需人工确认」的高风险动作，
            但 DO 过低禁止关增氧机这类硬性安全规则仍然拦截
        trigger_source: 指令来源，agent / manual / scheduled

    Returns:
        dict: 执行结果，包含 action、status、reason、blocked_by（若被拦截）
    """
    action = decision.get("action", "no_action")
    params = decision.get("params", {})
    reason = decision.get("reason", "")

    if action == "no_action":
        return {"action": action, "status": "skipped", "reason": "无需执行"}

    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    if not skip_cooldown and not _check_cooldown(pond_id, device_id, action):
        logger.info(f"冷却期未过，跳过指令: {action} | pond={pond_id}")
        return {
            "action": action,
            "status": "blocked",
            "blocked_by": "Cooldown",
            "reason": f"同一设备同一动作冷却期未过（{settings.COOLDOWN_MINUTES}分钟）"
        }

    current_status = _get_device_current_status(pond_id, action)
    if current_status == "running":
        if action.startswith("start_"):
            logger.info(f"设备已在运行，跳过启动指令: {action} | pond={pond_id}")
            return {
                "action": action,
                "status": "skipped",
                "reason": "设备已在运行"
            }

    if sensor_data is None:
        # 未显式指定时沿用 device_id；显式传空串表示按鱼塘查询
        query_device_id = device_id if sensor_device_id is None else sensor_device_id
        sensor_data = _get_latest_sensors(pond_id, query_device_id)

    safety_result = check_safety_rules(pond_id, device_id, action, sensor_data)
    if not safety_result.allowed:
        if safety_result.requires_approval and not human_confirmed:
            logger.warning(f"高风险动作进入待确认状态: {action} | pond={pond_id}")
            _save_pending_command(pond_id, device_id, action, params, reason, alert_type)
            return {
                "action": action,
                "status": "pending_approval",
                "reason": safety_result.reason
            }
        if not safety_result.requires_approval:
            logger.warning(
                f"安全规则拦截指令: {action} | pond={pond_id} | 原因={safety_result.reason}"
            )
            return {
                "action": action,
                "status": "blocked",
                "blocked_by": safety_result.blocked_by,
                "reason": safety_result.reason
            }
        # requires_approval 且 human_confirmed：操作员已在前端二次确认，放行
        logger.info(f"高风险动作已人工确认，继续执行: {action} | pond={pond_id}")

    logger.info(
        f"执行指令: {action} | pond={pond_id} | device={device_id} | "
        f"params={params} | 理由={reason}"
    )

    need_effect_check = action in _EFFECT_TRACKED_ACTIONS
    _insert_command(
        pond_id=pond_id,
        device_id=device_id,
        action=action,
        params=params,
        reason=reason,
        alert_type=alert_type,
        trigger_source=trigger_source,
        status="executed",
        result=_UNWIRED_RESULT,
        effect_tracked=need_effect_check,
        sensor_snapshot=sensor_data if need_effect_check else None,
        ts_str=ts_str,
    )

    # 指令已实际执行，同步设备状态供前端设备面板展示
    _sync_device_status(pond_id, action)

    return {"action": action, "status": "executed", "reason": reason}


def check_effects_due() -> int:
    """扫描到期的指令并执行效果检查（由 APScheduler 定时调用）

    Returns:
        int: 本次完成检查的指令数量
    """
    cutoff = (datetime.now() - timedelta(minutes=settings.EFFECT_CHECK_MINUTES))
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    rows = execute_mysql(
        """
        SELECT id, pond_id, device_id, command_type, sensor_snapshot, executed_at
        FROM device_commands
        WHERE effect_checked = 0 AND status = 'executed' AND executed_at <= %s
        ORDER BY id ASC
        LIMIT 100
        """,
        (cutoff_str,),
    )

    checked = 0
    for row in rows or []:
        try:
            _check_single_effect(row)
        except Exception as exc:
            logger.exception(f"效果检查异常: id={row.get('id')} | {exc}")
        finally:
            # 无论成功失败都标记已检查，避免同一条指令被反复扫描
            execute_mysql(
                "UPDATE device_commands SET effect_checked = 1 WHERE id = %s",
                (row["id"],),
            )
            checked += 1

    if checked:
        logger.info(f"本轮完成 {checked} 条指令的效果检查")
    return checked


def _check_single_effect(row: dict) -> None:
    """对单条指令执行效果验证

    Args:
        row: device_commands 查询结果行
    """
    pond_id = row["pond_id"]
    device_id = row["device_id"]
    action = row["command_type"]

    snapshot = row.get("sensor_snapshot")
    if not snapshot:
        return
    try:
        sensor_data_before = json.loads(snapshot)
    except (TypeError, ValueError):
        logger.warning(f"效果检查：执行前快照解析失败 id={row.get('id')}")
        return

    sensor_data_after = _get_latest_sensors(pond_id, device_id)
    if not sensor_data_after:
        # 人工指令记录的是控制设备标识（如 aerator_pond1），与采集设备ID不同源，
        # 按设备查不到时退回鱼塘维度，避免效果检查被静默跳过
        sensor_data_after = _get_latest_sensors(pond_id, "")
    if not sensor_data_after:
        logger.warning(f"效果检查：无法获取最新传感器数据 pond={pond_id}")
        return

    effect_ok = check_effect_tracking_rule(
        pond_id, device_id, action, sensor_data_before, sensor_data_after
    )

    if not effect_ok:
        logger.warning(
            f"效果检查失败: {action} 执行后指标未改善 | pond={pond_id} | "
            f"执行前={sensor_data_before} | 执行后={sensor_data_after}"
        )
        _escalate_alert(pond_id, device_id, action, sensor_data_before, sensor_data_after)


def _insert_command(pond_id: str, device_id: str, action: str, params: dict,
                    reason: str, alert_type: str, trigger_source: str,
                    status: str, result: str, effect_tracked: bool,
                    sensor_snapshot: Optional[Dict[str, float]], ts_str: str) -> None:
    """写入设备指令记录

    Args:
        effect_tracked: 是否需要后续效果检查。True 时 effect_checked=0，
                        由 check_effects_due 扫描；False 时直接置 1 表示无需检查
    """
    params_json = json.dumps(params, ensure_ascii=False)
    snapshot_json = json.dumps(sensor_snapshot, ensure_ascii=False) if sensor_snapshot else None
    executed_at = ts_str if status == "executed" else None

    execute_mysql(
        """
        INSERT INTO device_commands
        (ts, pond_id, device_id, command_type, trigger_source, trigger_alert_id,
         params, status, executed_at, result, effect_checked, sensor_snapshot)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (ts_str, pond_id, device_id, action, trigger_source, alert_type,
         params_json[:500], status, executed_at, result[:500],
         0 if effect_tracked else 1, snapshot_json),
    )


def _check_cooldown(pond_id: str, device_id: str, action: str) -> bool:
    """检查冷却期（依据 device_commands 中最近一条同类已执行指令）

    冷却状态落库而非放进程内存，服务重启与多实例部署下行为一致。

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        action: 动作类型

    Returns:
        bool: True=冷却期已过，可以执行；False=冷却期未过，跳过执行
    """
    cutoff = (
        datetime.now() - timedelta(minutes=settings.COOLDOWN_MINUTES)
    ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    rows = execute_mysql(
        """
        SELECT id FROM device_commands
        WHERE pond_id = %s AND device_id = %s AND command_type = %s
          AND status = 'executed' AND ts >= %s
        LIMIT 1
        """,
        (pond_id, device_id, action, cutoff),
    )
    return not rows


def _action_device_type(action: str) -> str:
    """从动作名推断设备类型

    Args:
        action: 动作类型，如 start_aerator

    Returns:
        str: 设备类型（aerator/pump/feeder），无法推断时返回空字符串
    """
    for device_type in ("aerator", "pump", "feeder"):
        if device_type in action:
            return device_type
    return ""


def _sync_device_status(pond_id: str, action: str) -> None:
    """指令执行后同步 control_devices 的运行状态

    原先只有 device_commands 落库，control_devices.status 永远停留在 online，
    导致前端设备面板无法反映真实运行状态。

    Args:
        pond_id: 鱼塘ID
        action: 动作类型（start_* 置为 running，stop_* 置回 online）
    """
    device_type = _action_device_type(action)
    if not device_type:
        return

    if action.startswith("start_"):
        new_status = "running"
    elif action.startswith("stop_"):
        new_status = "online"
    else:
        return

    execute_mysql(
        "UPDATE control_devices SET status = %s WHERE pond_id = %s AND device_type = %s",
        (new_status, pond_id, device_type),
    )


def _get_device_current_status(pond_id: str, action: str) -> str:
    """获取设备当前状态

    Args:
        pond_id: 鱼塘ID
        action: 动作类型（用于推断设备类型）

    Returns:
        str: "running"（运行中）、"stopped"（已停止）或 ""（未知）
    """
    device_type = _action_device_type(action)
    if not device_type:
        return ""

    sql = "SELECT status FROM control_devices WHERE pond_id = %s AND device_type = %s"
    rows = execute_mysql(sql, (pond_id, device_type))
    if rows and len(rows) > 0:
        return rows[0].get("status", "")
    return ""


def _get_latest_sensors(pond_id: str, device_id: str) -> Dict[str, float]:
    """获取最新传感器数据（用于安全校验与效果比对）

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID。传空字符串时按鱼塘取任意设备的最新一条，
                   用于人工控制场景（控制设备标识与采集设备ID不同源）

    Returns:
        dict: 所有传感器的最新值
    """
    conditions = ["pond_id = %s"]
    params = [pond_id]
    if device_id:
        conditions.append("device_id = %s")
        params.append(device_id)

    sql = f"""
        SELECT temperature, ph_value, dissolved_oxygen, ammonia_nitrogen,
               nitrite, ts_value, water_level
        FROM fishery.sensor_data
        WHERE {' AND '.join(conditions)}
        ORDER BY ts DESC LIMIT 1
    """
    result = execute_sql(sql, tuple(params))
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
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    _insert_command(
        pond_id=pond_id,
        device_id=device_id,
        action=action,
        params=params,
        reason=reason,
        alert_type=alert_type,
        trigger_source="agent",
        status="pending_approval",
        result="待人工确认",
        effect_tracked=False,
        sensor_snapshot=None,
        ts_str=ts_str,
    )


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
    now = datetime.now()
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    before_str = json.dumps(sensor_data_before, ensure_ascii=False)[:200]
    after_str = json.dumps(sensor_data_after, ensure_ascii=False)[:200]
    message = f"指令执行效果未达标: {action} | 执行前: {before_str} | 执行后: {after_str}"

    execute_mysql(
        """
        INSERT INTO alerts
        (ts, pond_id, device_id, alert_type, alert_value, threshold_value, message, is_read)
        VALUES (%s, %s, %s, 'effect_check_failed', 0, 0, %s, 0)
        """,
        (ts_str, pond_id, device_id, message[:500]),
    )

    from app.tools.alert_tracker import _create_manual_task
    _create_manual_task(pond_id, "effect_check_failed", {
        "alert_value": 0,
        "threshold_value": 0,
        "started_at": ts_str
    }, now)

    logger.warning(f"告警已升级并创建人工工单: pond={pond_id} action={action}")
