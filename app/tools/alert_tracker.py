"""告警持续追踪：定时扫描活跃告警，超时（默认60分钟）自动升级为人工工单

核心功能：
1. 使用 APScheduler 定时扫描 alert_duration 表中的活跃告警
2. 计算每个告警的持续时间（当前时间 - started_at）
3. 持续时间超过 ESCALATION_MINUTES（默认60分钟）时自动升级
4. 升级操作：标记告警为 escalated + 创建人工工单
5. 顺带驱动设备指令的效果检查（避免在进程内用 sleep 挂 15 分钟）
6. 兜底关闭已恢复的告警：主链路在 data_processor 中按连续正常点数关闭，
   这里用最新传感器值再检测一遍，覆盖进程重启丢计数、历史遗留数据的情况

配置参数（来自 settings）：
- SCAN_INTERVAL_SECONDS: 扫描间隔（默认30秒）
- ESCALATION_MINUTES: 升级阈值（默认60分钟）
- EFFECT_CHECK_MINUTES: 指令效果检查延迟（默认15分钟）
- RECOVERY_SCAN_INTERVAL_SECONDS: 恢复兜底扫描间隔（默认300秒）
- RECOVERY_DATA_MAX_AGE_SECONDS: 兜底扫描认可的数据新鲜度（默认300秒）

告警状态流转：
active（活跃）→ escalated（已升级）→ resolved（已解决/已恢复）

说明：alert_duration 存于 MySQL，可直接 UPDATE，
不再需要「DELETE + INSERT 模拟更新」，也就不会在并发下丢记录。
"""

import logging
from collections import deque
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from dateutil.parser import parse as parse_datetime

from app.core.config import settings
from app.core.db import execute_mysql, execute_sql
from app.tools.detector import check as check_threshold

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

# detector 可能产出的告警类型后缀，用于从 alert_type 反推传感器字段
_ALERT_TYPE_SUFFIXES = (
    "low_warning",
    "high_warning",
    "trend_rising",
    "trend_falling",
    "low",
    "high",
)

# 传感器字段（与 sensor_data 表列名一致）
_SENSOR_COLUMNS = (
    "temperature", "ph_value", "dissolved_oxygen", "ammonia_nitrogen",
    "nitrite", "ts_value", "water_level",
)


def _parse_time_value(value):
    """把数据库/接口返回的时间值统一成无时区 datetime

    MySQL 一般直接返回 datetime 对象，TDengine REST 返回 RFC3339 字符串或毫秒时间戳，
    这里统一兼容，避免不同来源的时间无法比较。

    Args:
        value: 时间字段值

    Returns:
        datetime: 解析后的 datetime 对象（无时区），失败返回 None
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)

    if value is None:
        return None

    # TDengine REST 有时以毫秒时间戳返回 ts
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None

    try:
        parsed = parse_datetime(str(value))
        return parsed.replace(tzinfo=None)
    except Exception:
        return None


def _parse_sensor_type(alert_type: str) -> str:
    """从告警类型反推传感器字段

    例：dissolved_oxygen_low_warning → dissolved_oxygen
        effect_check_failed → ""（非传感器类告警，不参与恢复判断）

    Args:
        alert_type: 告警类型

    Returns:
        str: 传感器字段名，无法识别时返回空字符串
    """
    if not alert_type:
        return ""
    for suffix in _ALERT_TYPE_SUFFIXES:
        if alert_type.endswith("_" + suffix):
            return alert_type[: -(len(suffix) + 1)]
    return ""


def resolve_sensor_alerts(pond_id: str, device_id: str, sensor_type: str) -> int:
    """把某个传感器已恢复正常的活跃告警置为 resolved

    同时覆盖 active 与 escalated：指标恢复后告警本身应关闭，
    已升级的人工工单仍保留在 manual_tasks 中由人工闭环。

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID
        sensor_type: 传感器字段名（如 dissolved_oxygen）

    Returns:
        int: 本次关闭的告警条数
    """
    if not sensor_type:
        return 0

    alert_types = [f"{sensor_type}_{suffix}" for suffix in _ALERT_TYPE_SUFFIXES]
    placeholders = ", ".join(["%s"] * len(alert_types))

    rows = execute_mysql(
        f"""
        SELECT id, alert_type FROM alert_duration
        WHERE pond_id = %s AND device_id = %s
          AND status IN ('active', 'escalated')
          AND alert_type IN ({placeholders})
        """,
        (pond_id, device_id, *alert_types),
    )
    if not rows:
        return 0

    ids = [row["id"] for row in rows]
    id_placeholders = ", ".join(["%s"] * len(ids))
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    execute_mysql(
        f"""
        UPDATE alert_duration
        SET status = 'resolved', last_checked_at = %s
        WHERE id IN ({id_placeholders}) AND status IN ('active', 'escalated')
        """,
        (now_str, *ids),
    )

    logger.warning(
        f"告警恢复关闭: pond={pond_id} device={device_id} sensor={sensor_type} "
        f"共 {len(ids)} 条 ({', '.join(row['alert_type'] for row in rows)})"
    )
    return len(ids)


def _get_latest_sensors_with_time(pond_id: str, device_id: str) -> dict:
    """查询指定鱼塘/设备的最新一条传感器数据（含采集时间）

    Args:
        pond_id: 鱼塘ID
        device_id: 设备ID

    Returns:
        dict: {ts: datetime, temperature: float, ...}，无数据时返回 {}
    """
    sql = f"""
        SELECT ts, {', '.join(_SENSOR_COLUMNS)}
        FROM fishery.sensor_data
        WHERE pond_id = %s AND device_id = %s
        ORDER BY ts DESC LIMIT 1
    """
    result = execute_sql(sql, (pond_id, device_id))
    if result.get("code") != 0 or not result.get("data"):
        return {}

    row = result["data"][0]
    data = dict(zip(("ts", *_SENSOR_COLUMNS), row))
    data["ts"] = _parse_time_value(data.get("ts"))
    return data


def _scan_recovered_alerts() -> int:
    """兜底扫描：用最新传感器值重新检测，关闭已恢复的活跃告警

    主链路（DataProcessor.check_alert）在连续 N 个采样点正常后就会关闭告警，
    这里覆盖两类漏网场景：
    1. 进程重启导致恢复计数清零
    2. 逻辑上线前遗留的 active/escalated 记录

    Returns:
        int: 本次关闭的告警条数
    """
    rows = execute_mysql(
        """
        SELECT id, pond_id, device_id, alert_type
        FROM alert_duration
        WHERE status IN ('active', 'escalated')
        ORDER BY id DESC
        LIMIT 200
        """
    )
    if not rows:
        return 0

    # 同一 (pond, device) 只查一次最新数据
    sensor_cache: dict[tuple[str, str], dict] = {}
    resolved = 0

    for row in rows:
        sensor_type = _parse_sensor_type(row.get("alert_type", ""))
        if not sensor_type:
            continue

        pond_id = row.get("pond_id", "")
        device_id = row.get("device_id", "")
        cache_key = (pond_id, device_id)

        if cache_key not in sensor_cache:
            sensor_cache[cache_key] = _get_latest_sensors_with_time(pond_id, device_id)

        latest = sensor_cache[cache_key]
        if not latest:
            continue

        # 设备离线或数据过期时不做判断，避免用过期值误关告警
        ts: Optional[datetime] = latest.get("ts")
        if not ts:
            continue
        if (datetime.now() - ts).total_seconds() > settings.RECOVERY_DATA_MAX_AGE_SECONDS:
            continue

        value = latest.get(sensor_type)
        if not isinstance(value, (int, float)):
            continue

        # 传空历史窗口：只做阈值判断，趋势类告警交给主链路处理
        if check_threshold(pond_id, device_id, sensor_type, value, deque()) is None:
            resolved += resolve_sensor_alerts(pond_id, device_id, sensor_type)

    if resolved:
        logger.info(f"恢复兜底扫描完成，共关闭 {resolved} 条告警")
    return resolved


def _scan_and_escalate():
    """扫描所有活跃告警，检查是否需要升级

    执行流程：
    1. 查询 alert_duration 表中 status='active' 的记录
    2. 遍历每条记录，计算持续时间
    3. 更新 last_checked_at 和 duration_minutes
    4. 若持续时间 >= ESCALATION_MINUTES，执行升级操作：
       - 更新状态为 'escalated'
       - 创建人工工单
    """
    now = datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')

    rows = execute_mysql(
        """
        SELECT id, pond_id, device_id, alert_type, alert_value, threshold_value,
               started_at, duration_minutes, status
        FROM alert_duration
        WHERE status = 'active'
        """
    )

    for record in rows or []:
        started_at = _parse_time_value(record.get("started_at"))
        if not started_at:
            continue

        duration = (now - started_at).total_seconds() / 60
        duration_minutes = int(duration)

        if duration >= settings.ESCALATION_MINUTES:
            logger.warning(
                f"告警超时升级: pond={record['pond_id']} "
                f"type={record['alert_type']} 持续 {duration_minutes} 分钟"
            )
            _create_manual_task(record["pond_id"], record["alert_type"], record, now)
            execute_mysql(
                """
                UPDATE alert_duration
                SET status = 'escalated', last_checked_at = %s, duration_minutes = %s
                WHERE id = %s AND status = 'active'
                """,
                (now_str, duration_minutes, record["id"]),
            )
        else:
            execute_mysql(
                """
                UPDATE alert_duration
                SET last_checked_at = %s, duration_minutes = %s
                WHERE id = %s
                """,
                (now_str, duration_minutes, record["id"]),
            )


def _scan_effect_checks():
    """驱动到期设备指令的效果检查

    单独的调度任务，避免把 15 分钟的延迟挂在业务流程里。
    """
    from app.tools.executor import check_effects_due

    try:
        check_effects_due()
    except Exception as e:
        logger.exception(f"效果检查扫描异常: {e}")


def _create_manual_task(pond_id: str, alert_type: str, record: dict, now: datetime):
    """创建人工工单

    Args:
        pond_id: 鱼塘ID
        alert_type: 告警类型
        record: alert_duration 表的记录字典
        now: 当前时间（用于记录 escalated_at）

    Note:
        创建前会检查是否已存在同类型的 pending 工单，避免重复创建
        工单创建在 MySQL 的 manual_tasks 表中
    """
    alert_value = record.get("alert_value", 0)
    threshold_value = record.get("threshold_value", 0)
    started_at = record.get("started_at")

    if isinstance(started_at, datetime):
        started_at_str = started_at.strftime('%Y-%m-%d %H:%M:%S')
    else:
        parsed = _parse_time_value(started_at)
        started_at_str = parsed.strftime('%Y-%m-%d %H:%M:%S') if parsed else None

    now_str = now.strftime('%Y-%m-%d %H:%M:%S')

    check_sql = """
        SELECT id FROM manual_tasks
        WHERE pond_id = %s AND alert_type = %s AND status = 'pending'
        ORDER BY created_at DESC LIMIT 1
    """
    existing = execute_mysql(check_sql, (pond_id, alert_type))
    if existing and len(existing) > 0:
        logger.debug(f"工单已存在，跳过创建: pond={pond_id} type={alert_type}")
        return

    insert_sql = """
        INSERT INTO manual_tasks (pond_id, alert_type, alert_value, threshold_value, started_at, escalated_at, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending')
    """
    execute_mysql(insert_sql, (pond_id, alert_type, alert_value, threshold_value, started_at_str, now_str))
    logger.warning(f"人工工单已创建: pond={pond_id} type={alert_type}")


def _scan_agent_health() -> None:
    """刷新 Agent 决策健康度（fallback 占比）；监控失败不影响其它扫描任务"""
    from app.agent.monitor import check

    try:
        check()
    except Exception as exc:
        logger.warning(f"Agent 健康度扫描失败: {exc}")


def start_tracker():
    """启动告警追踪器

    创建后台调度器，每隔 SCAN_INTERVAL_SECONDS 秒执行：
    1. 告警超时升级扫描
    2. 设备指令效果检查
    3. 告警恢复兜底扫描（独立间隔，频率更低）
    4. Agent 决策健康度扫描（独立间隔）

    Note:
        使用单例模式，避免重复启动多个调度器
        调度器使用 BackgroundScheduler，运行在后台线程中
    """
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _scan_and_escalate, 'interval',
        seconds=settings.SCAN_INTERVAL_SECONDS, id='alert_tracker',
    )
    _scheduler.add_job(
        _scan_effect_checks, 'interval',
        seconds=settings.SCAN_INTERVAL_SECONDS, id='effect_checker',
    )
    _scheduler.add_job(
        _scan_recovered_alerts, 'interval',
        seconds=settings.RECOVERY_SCAN_INTERVAL_SECONDS, id='alert_recovery',
    )
    _scheduler.add_job(
        _scan_agent_health, 'interval',
        seconds=settings.AGENT_HEALTH_SCAN_SECONDS, id='agent_health',
    )
    _scheduler.start()
    logger.info(
        f"告警追踪器已启动 | 扫描间隔 {settings.SCAN_INTERVAL_SECONDS}s | "
        f"升级阈值 {settings.ESCALATION_MINUTES}min | "
        f"效果检查延迟 {settings.EFFECT_CHECK_MINUTES}min | "
        f"恢复兜底扫描 {settings.RECOVERY_SCAN_INTERVAL_SECONDS}s | "
        f"Agent 健康度扫描 {settings.AGENT_HEALTH_SCAN_SECONDS}s"
    )


def stop_tracker():
    """停止告警追踪器

    关闭后台调度器，释放资源

    Note:
        wait=False 参数表示不等待正在执行的任务完成，立即关闭
        适用于应用关闭时的清理场景
    """
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("告警追踪器已停止")
