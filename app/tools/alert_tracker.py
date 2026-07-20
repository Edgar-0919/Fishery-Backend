"""告警持续追踪：定时扫描活跃告警，超时（默认60分钟）自动升级为人工工单

核心功能：
1. 使用 APScheduler 定时扫描 alert_duration 表中的活跃告警
2. 计算每个告警的持续时间（当前时间 - started_at）
3. 持续时间超过 ESCALATION_MINUTES（默认60分钟）时自动升级
4. 升级操作：标记告警为 escalated + 创建人工工单

配置参数（来自 settings）：
- SCAN_INTERVAL_SECONDS: 扫描间隔（默认30秒）
- ESCALATION_MINUTES: 升级阈值（默认60分钟）

告警状态流转：
active（活跃）→ escalated（已升级）→ resolved（已解决）
"""

from datetime import datetime
from dateutil.parser import parse as parse_datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.db import execute_sql, execute_mysql
from app.core.config import settings

_scheduler: BackgroundScheduler | None = None


def _parse_time_value(value):
    """解析 TDengine 返回的时间值

    TDengine 返回的时间字段可能有多种格式：
    1. datetime 对象（直接使用）
    2. ISO 8601 字符串：2026-07-17T22:55:39.000Z
    3. 普通时间字符串：2026-07-17 22:55:39

    注意：所有返回的 datetime 都转为无时区感知（offset-naive），
    以便与 datetime.now() 进行比较和计算

    Args:
        value: TDengine 返回的时间值

    Returns:
        datetime: 解析后的 datetime 对象（无时区），失败返回 None
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)

    if value is None:
        return None

    try:
        parsed = parse_datetime(str(value))
        return parsed.replace(tzinfo=None)
    except Exception:
        return None


def _scan_and_escalate():
    """扫描所有活跃告警，检查是否需要升级

    执行流程：
    1. 查询 alert_duration 表中 status='active' 的记录
    2. 遍历每条记录，计算持续时间
    3. 更新 last_checked_at 和 duration_minutes
    4. 若持续时间 >= ESCALATION_MINUTES，执行升级操作：
       - 更新状态为 'escalated'
       - 创建人工工单

    Note:
        TDengine 返回的字符串字段可能包含二进制数据（末尾\x00填充），
        需要通过 _strip_binary 函数清理
    """
    now = datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')

    sql = """
        SELECT ts, pond_id, device_id, alert_type, alert_value, threshold_value,
               started_at, last_checked_at, duration_minutes, status
        FROM fishery.alert_duration
        WHERE status = 'active'
    """
    result = execute_sql(sql)
    if result.get("code") != 0 or not result.get("data"):
        return

    columns = [col[0] for col in result.get("column_meta", [])]
    for row in result["data"]:
        record = dict(zip(columns, row))
        started_at = _parse_time_value(record.get("started_at"))
        if not started_at:
            continue

        duration = (now - started_at).total_seconds() / 60

        pond_id = _strip_binary(record["pond_id"])
        device_id = _strip_binary(record["device_id"])
        alert_type = _strip_binary(record["alert_type"])
        ts_value = record.get("ts")
        if ts_value:
            ts_parsed = _parse_time_value(ts_value)
            ts_str = ts_parsed.strftime('%Y-%m-%d %H:%M:%S') if ts_parsed else str(ts_value)
        else:
            continue

        current_status = record.get("status")
        current_status = _strip_binary(current_status) if current_status else "active"

        if duration >= settings.ESCALATION_MINUTES:
            new_status = "escalated"
            print(f"[TRACKER] 告警超时升级: pond={pond_id} type={alert_type} 持续 {int(duration)} 分钟")
            _create_manual_task(pond_id, alert_type, record, now)
        else:
            new_status = current_status

        delete_sql = f"DELETE FROM fishery.alert_duration WHERE ts = '{ts_str}'"
        execute_sql(delete_sql)

        insert_sql = f"""
            INSERT INTO fishery.alert_duration
            (ts, pond_id, device_id, alert_type, alert_value, threshold_value,
             started_at, last_checked_at, duration_minutes, status, agent_decision, agent_action)
            VALUES ('{ts_str}', '{pond_id}', '{device_id}', '{alert_type}',
                    {record.get('alert_value', 0)}, {record.get('threshold_value', 0)},
                    '{record.get('started_at', ts_str)}', '{now_str}', {int(duration)},
                    '{new_status}', '{_strip_binary(record.get('agent_decision', ''))}',
                    '{_strip_binary(record.get('agent_action', ''))}')
        """
        execute_sql(insert_sql)


def _strip_binary(val) -> str:
    """清理 TDengine 返回的二进制字符串数据

    TDengine 的 BINARY 类型字段返回时可能包含：
    - bytes 类型，末尾有 \x00 填充
    - str 类型，末尾有 \x00 填充

    Args:
        val: TDengine 返回的字段值（bytes 或 str）

    Returns:
        str: 清理后的纯字符串
    """
    if isinstance(val, bytes):
        return val.decode('utf-8').rstrip('\x00').strip()
    if isinstance(val, str):
        return val.rstrip('\x00').strip()
    return str(val)


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
        try:
            parsed = _parse_time_value(started_at)
            started_at_str = parsed.strftime('%Y-%m-%d %H:%M:%S') if parsed else None
        except Exception:
            started_at_str = None

    now_str = now.strftime('%Y-%m-%d %H:%M:%S')

    check_sql = """
        SELECT id FROM manual_tasks
        WHERE pond_id = %s AND alert_type = %s AND status = 'pending'
        ORDER BY created_at DESC LIMIT 1
    """
    existing = execute_mysql(check_sql, (pond_id, alert_type))
    if existing and len(existing) > 0:
        print(f"[TRACKER] 工单已存在，跳过创建: pond={pond_id} type={alert_type}")
        return

    insert_sql = """
        INSERT INTO manual_tasks (pond_id, alert_type, alert_value, threshold_value, started_at, escalated_at, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending')
    """
    execute_mysql(insert_sql, (pond_id, alert_type, alert_value, threshold_value, started_at_str, now_str))
    print(f"[TRACKER] 人工工单已创建: pond={pond_id} type={alert_type}")


def start_tracker():
    """启动告警追踪器

    创建后台调度器，每隔 SCAN_INTERVAL_SECONDS 秒执行一次扫描任务

    Note:
        使用单例模式，避免重复启动多个调度器
        调度器使用 BackgroundScheduler，运行在后台线程中
    """
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_scan_and_escalate, 'interval', seconds=settings.SCAN_INTERVAL_SECONDS, id='alert_tracker')
    _scheduler.start()
    print(f"[TRACKER] 告警追踪器已启动，扫描间隔 {settings.SCAN_INTERVAL_SECONDS}s，升级阈值 {settings.ESCALATION_MINUTES}min")


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
        print("[TRACKER] 告警追踪器已停止")