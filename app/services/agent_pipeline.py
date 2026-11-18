"""Agent 决策链路的线程调度与上下文查询

核心流程：
1. 查询同一鱼塘所有传感器的最新值（全局视图）
2. 查询设备当前状态（增氧机/水泵是否运行）
3. 查询最近 10 分钟该指标的历史数据（从 MySQL 时序表）
4. 交给 app.agent.pipeline 跑 ReAct 循环（工具调用 + Guard 校验 + 统一执行出口）
5. 更新 alert_duration 表（新建或更新活跃告警记录）

调用时机：当 detector 检测到 critical 级别告警时，由 data_processor 提交到线程池

线程模型：
- 本模块是纯同步实现，由 `submit_decision_pipeline` 提交到专用线程池执行。
- 多轮 LLM 调用耗时长，绝不能在事件循环中直接调用，否则整个 FastAPI
  的所有接口都会被阻塞。
"""

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from app.core.config import settings
from app.core.db import execute_mysql, execute_sql, safe_identifier

logger = logging.getLogger(__name__)

# Agent 循环使用独立线程池：多轮 LLM 调用耗时长，不能占用数据接收线程
_AGENT_EXECUTOR = ThreadPoolExecutor(
    max_workers=settings.AGENT_MAX_WORKERS,
    thread_name_prefix="agent-loop",
)


def submit_decision_pipeline(alert) -> Future:
    """把一次决策提交到线程池，立即返回不阻塞数据接收

    紧急规则分支在 data_processor 中，与此函数无关，
    因此这里的任何改动都不会影响「紧急规则绕过 LLM」这条硬防线。

    Args:
        alert: AlertEvent 告警事件

    Returns:
        Future: 可用于观测执行结果
    """
    from app.agent.pipeline import agent_loop_pipeline

    return _AGENT_EXECUTOR.submit(agent_loop_pipeline, alert)


def shutdown_decision_executor(wait: bool = False) -> None:
    """关闭决策线程池（应用退出时调用）"""
    _AGENT_EXECUTOR.shutdown(wait=wait)


def _upsert_alert_duration(alert, analysis: str, action: str, now: datetime) -> None:
    """写入或更新活跃告警记录

    用 MySQL UPDATE 替代原来的 TDengine DELETE + INSERT：
    - 不再有读改写竞态，也不会因同秒主键冲突覆盖其他指标的告警
    - duration_minutes / started_at 由原记录保留，只刷新最后检查时间与决策内容

    Args:
        alert: AlertEvent 告警事件
        analysis: LLM 分析文本
        action: 决策动作
        now: 当前时间
    """
    ts_str = now.strftime('%Y-%m-%d %H:%M:%S')
    safe_analysis = (analysis or "")[:500]
    safe_action = (action or "")[:100]

    # 同时匹配 active 与 escalated：告警升级后若指标仍未恢复，后续决策继续复用
    # 同一条记录，避免每轮决策都新建一条 active，导致告警列表重复堆积
    existing = execute_mysql(
        """
        SELECT id, started_at FROM alert_duration
        WHERE pond_id = %s AND device_id = %s AND alert_type = %s
          AND status IN ('active', 'escalated')
        ORDER BY id DESC LIMIT 1
        """,
        (alert.pond_id, alert.device_id, alert.alert_type),
    )

    if existing:
        row = existing[0]
        started_at = row.get("started_at")
        if not isinstance(started_at, datetime):
            started_at = now
        duration = max(0, int((now - started_at).total_seconds() / 60))
        execute_mysql(
            """
            UPDATE alert_duration
            SET alert_value = %s, threshold_value = %s, last_checked_at = %s,
                duration_minutes = %s, agent_decision = %s, agent_action = %s
            WHERE id = %s
            """,
            (alert.value, alert.threshold, ts_str, duration,
             safe_analysis, safe_action, row["id"]),
        )
        logger.debug(f"告警已存在，更新决策记录: {alert.alert_type}")
        return

    execute_mysql(
        """
        INSERT INTO alert_duration
        (pond_id, device_id, alert_type, alert_value, threshold_value,
         started_at, last_checked_at, duration_minutes, status, agent_decision, agent_action)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 'active', %s, %s)
        """,
        (alert.pond_id, alert.device_id, alert.alert_type, alert.value, alert.threshold,
         ts_str, ts_str, safe_analysis, safe_action),
    )


def _get_all_sensors_latest(pond_id: str, device_id: str) -> dict:
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
    sql = """
        SELECT temperature, ph_value, dissolved_oxygen, ammonia_nitrogen,
               nitrite, ts_value, water_level
        FROM fishery.sensor_data
        WHERE pond_id = %s AND device_id = %s
        ORDER BY ts DESC LIMIT 1
    """
    result = execute_sql(sql, (pond_id, device_id))
    if result.get("code") == 0 and result.get("data") and len(result["data"]) > 0:
        columns = ["temperature", "ph_value", "dissolved_oxygen", "ammonia_nitrogen",
                   "nitrite", "ts_value", "water_level"]
        row = result["data"][0]
        return dict(zip(columns, row))
    return {}


def _get_device_status(pond_id: str) -> dict:
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
    for row in rows or []:
        status[row.get("device_type", "")] = row.get("status", "")
    return status
