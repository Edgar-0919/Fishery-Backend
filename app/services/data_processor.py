"""数据处理服务：数据清洗、缺失值填充、滑动窗口、入库、告警触发

核心职责：
1. 接收传感器原始数据，进行合法性校验和清洗
2. 使用滑动窗口维护最近的传感器历史值
3. 对缺失数据进行均值填充
4. 将清洗后的数据写入 MySQL
5. 触发异常检测，critical 级别告警自动启动 Agent 决策
"""

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from app.core.config import settings
from app.core.db import execute_sql, execute_mysql
from app.tools.detector import check

logger = logging.getLogger(__name__)


class DataProcessor:
    """传感器数据处理核心类

    管理传感器数据的完整生命周期：清洗 → 填充 → 入库 → 告警检测
    使用滑动窗口维护最近 10 条历史数据，用于波动率检测和缺失值填充
    """

    def __init__(self):
        """初始化数据处理器

        _sensor_history: 滑动窗口字典，key 格式为 "{pond_id}:{device_id}:{sensor_type}"
                         value 为 deque，最多保存最近 10 条 (时间戳ms, 值) 元组
        _recovery_counters: 连续正常采样点计数，达到 RECOVERY_CONSECUTIVE_POINTS
                            后关闭该指标对应的活跃告警，避免临界值抖动反复开关
        """
        self._sensor_history: dict[str, deque] = {}
        self._recovery_counters: dict[str, int] = {}

    @staticmethod
    def _history_key(pond_id: str, device_id: str, sensor_type: str) -> str:
        """滑动窗口与恢复计数的统一键"""
        return f"{pond_id}:{device_id}:{sensor_type}"

    def _update_history(self, pond_id: str, device_id: str, sensor_type: str, value: float, ts_ms: int):
        """更新传感器历史数据滑动窗口

        Args:
            pond_id: 鱼塘ID
            device_id: 设备ID
            sensor_type: 传感器类型（如 temperature）
            value: 传感器数值
            ts_ms: 时间戳（毫秒）
        """
        key = f"{pond_id}:{device_id}:{sensor_type}"
        if key not in self._sensor_history:
            self._sensor_history[key] = deque(maxlen=10)
        self._sensor_history[key].append((ts_ms, value))

    def _get_history(self, pond_id: str, device_id: str, sensor_type: str) -> deque:
        """获取指定传感器的历史数据滑动窗口

        Args:
            pond_id: 鱼塘ID
            device_id: 设备ID
            sensor_type: 传感器类型

        Returns:
            deque: 包含 (时间戳ms, 值) 元组的双端队列，最多 10 条记录
        """
        key = f"{pond_id}:{device_id}:{sensor_type}"
        return self._sensor_history.get(key, deque())

    def clean_sensor_value(self, pond_id: str, device_id: str, sensor_type: str, value) -> Optional[float]:
        """清洗传感器数值

        执行两项检查：
        1. 范围校验：根据 settings.SENSOR_RANGES 判断是否在合法范围内
        2. 波动率检测：与上一个采样点对比，变化率超过 50% 标记为可疑（仅告警不丢弃）

        Args:
            pond_id: 鱼塘ID
            device_id: 设备ID
            sensor_type: 传感器类型
            value: 待清洗的原始值

        Returns:
            Optional[float]: 清洗后的有效值，范围越界时返回 None
        """
        if sensor_type not in settings.SENSOR_RANGES:
            return value

        min_val, max_val = settings.SENSOR_RANGES[sensor_type]

        if value < min_val or value > max_val:
            logger.warning(
                f"异常值丢弃: pond={pond_id} dev={device_id} {sensor_type}={value} "
                f"(范围 {min_val}~{max_val})"
            )
            return None

        history = self._get_history(pond_id, device_id, sensor_type)
        if history:
            prev_val = history[-1][1]
            if prev_val != 0:
                change_rate = abs(value - prev_val) / abs(prev_val)
                if change_rate > 0.5:
                    logger.warning(
                        f"波动率异常: pond={pond_id} dev={device_id} {sensor_type} "
                        f"{prev_val:.2f} → {value:.2f} (变化率 {change_rate:.1%})"
                    )

        return value

    def fill_missing_value(self, pond_id: str, device_id: str, sensor_type: str) -> Optional[float]:
        """缺失值填充：使用最近 5 个有效采样点的均值填充

        Args:
            pond_id: 鱼塘ID
            device_id: 设备ID
            sensor_type: 传感器类型

        Returns:
            Optional[float]: 填充后的均值，历史数据不足 3 条时返回 None

        Note:
            历史数据不足 3 条时不填充，避免计算出不可靠的均值
            取最近 5 条而非全部 10 条，更能反映当前趋势
        """
        history = self._get_history(pond_id, device_id, sensor_type)
        if len(history) < 3:
            return None
        recent = [v for _, v in list(history)[-5:]]
        avg = sum(recent) / len(recent)
        logger.debug(
            f"缺失值填充: pond={pond_id} dev={device_id} {sensor_type} "
            f"→ {avg:.2f} (窗口={len(recent)})"
        )
        return round(avg, 2)

    def get_pond_id(self, device_id: str) -> Optional[str]:
        """根据设备ID查询绑定的鱼塘ID

        Args:
            device_id: 设备ID

        Returns:
            Optional[str]: 绑定的鱼塘ID，未绑定时返回 None

        Note:
            查询 MySQL 的 device_mapping 表，该表维护设备与鱼塘的映射关系
        """
        sql = "SELECT pond_id FROM device_mapping WHERE device_id = %s"
        rows = execute_mysql(sql, (device_id,))
        if rows and len(rows) > 0:
            return rows[0]['pond_id']
        return None

    def process_one_message(self, data: dict):
        """处理一条消息，根据消息类型分发到不同处理逻辑

        消息类型判断优先级：
        1. type=1 或 notifyType='property' 或 messageType='notify' → 传感器数据
        2. type=2 或 notifyType='deviceStatus' → 设备状态变更
        3. 其他 → 未知消息，打印日志

        Args:
            data: OneNET 推送的原始消息字典
        """
        msg_type = data.get('type')
        notify_type = data.get('notifyType')
        message_type = data.get('messageType')

        if msg_type == 1 or (notify_type == 'property' or message_type == 'notify'):
            self.process_data_point(data)
        elif msg_type == 2 or notify_type == 'deviceStatus':
            self.process_device_status(data)
        else:
            logger.warning(
                f"未知消息类型: type={msg_type}, notifyType={notify_type}, messageType={message_type}"
            )

    def process_data_point(self, data: dict):
        """处理单个传感器数据点（核心流程）

        完整处理流程：
        1. 提取设备ID、产品ID、参数数据
        2. 查询设备绑定的鱼塘ID
        3. 遍历传感器参数，进行清洗
        4. 对缺失的指标进行均值填充
        5. 更新滑动窗口历史
        6. 组装 SQL 写入 MySQL
        7. 对每个指标触发异常检测，critical 级别启动 Agent 决策

        Args:
            data: 包含设备信息和传感器参数的字典

        Note:
            传感器参数映射：
            - temp → temperature (水温)
            - pH → ph_value (pH值)
            - DO → dissolved_oxygen (溶解氧)
            - NH3 → ammonia_nitrogen (氨氮)
            - NO2 → nitrite (亚硝酸盐)
            - TS → ts_value (浊度)
            - WL → water_level (水位)
        """
        device_id = data.get('deviceName', data.get('dev_name', data.get('dev_id', 'unknown')))
        product_id = data.get('productId', 'unknown')
        params = data.get('data', {}).get('params', {})

        pond_id = self.get_pond_id(device_id)
        if not pond_id:
            logger.warning(f"设备未绑定: {device_id}")
            return

        sensor_data_map = {
            'temp': 'temperature',
            'pH': 'ph_value',
            'DO': 'dissolved_oxygen',
            'NH3': 'ammonia_nitrogen',
            'NO2': 'nitrite',
            'TS': 'ts_value',
            'WL': 'water_level',
        }

        collected: dict[str, float] = {}
        ts_ms = None

        for param_key, field_name in sensor_data_map.items():
            if param_key in params:
                param = params[param_key]
                value = param.get('value')
                clean_value = self.clean_sensor_value(pond_id, device_id, field_name, value)
                if clean_value is not None:
                    collected[field_name] = clean_value
                    if ts_ms is None:
                        ts_ms = param.get('time', int(datetime.now().timestamp() * 1000))

        if not collected:
            logger.warning(f"无有效传感器数据: {device_id}")
            return

        for field_name in sensor_data_map.values():
            if field_name not in collected:
                filled = self.fill_missing_value(pond_id, device_id, field_name)
                if filled is not None:
                    collected[field_name] = filled

        ts_str = datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')
        ts_ns = ts_ms * 1000000

        for field_name, value in collected.items():
            self._update_history(pond_id, device_id, field_name, value, ts_ms)

        t = collected.get('temperature', 'NULL')
        p = collected.get('ph_value', 'NULL')
        do = collected.get('dissolved_oxygen', 'NULL')
        nh = collected.get('ammonia_nitrogen', 'NULL')
        no = collected.get('nitrite', 'NULL')
        ts_v = collected.get('ts_value', 'NULL')
        wl = collected.get('water_level', 'NULL')

        sql = """
            INSERT INTO fishery.sensor_data
            (ts, pond_id, device_id, product_id,
             temperature, ph_value, dissolved_oxygen, ammonia_nitrogen,
             nitrite, ts_value, water_level)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        params = (
            ts_str, pond_id, device_id, product_id,
            collected.get('temperature'), collected.get('ph_value'),
            collected.get('dissolved_oxygen'), collected.get('ammonia_nitrogen'),
            collected.get('nitrite'), collected.get('ts_value'),
            collected.get('water_level'),
        )
        execute_sql(sql, params)
        logger.info(
            f"数据入库: pond={pond_id} dev={device_id} | temp={t} pH={p} DO={do} "
            f"NH3={nh} NO2={no} TS={ts_v} WL={wl}"
        )

        from app.tools.rules import check_emergency_rules
        emergency_result = check_emergency_rules(pond_id, device_id, collected)
        if emergency_result:
            logger.warning(f"紧急规则命中: {emergency_result.reason}")
            from app.tools.executor import execute
            execute(pond_id, device_id, {
                "action": emergency_result.action,
                "params": emergency_result.params,
                "reason": emergency_result.reason
            }, f"{emergency_result.action}_emergency")
        else:
            for field_name, value in collected.items():
                alert = self.check_alert(pond_id, device_id, field_name, value, ts_ns)
                if alert and alert.severity == "critical":
                    from app.services.agent_pipeline import submit_decision_pipeline
                    submit_decision_pipeline(alert)

    def _handle_recovery(self, pond_id: str, device_id: str, sensor_type: str) -> None:
        """累计连续正常采样点，达到阈值后关闭该指标的活跃告警

        Args:
            pond_id: 鱼塘ID
            device_id: 设备ID
            sensor_type: 传感器类型

        Note:
            单点正常不足以判定恢复：5 秒上报下溶解氧可能刚好在 3.0 附近抖动，
            连续 RECOVERY_CONSECUTIVE_POINTS 个点都正常才关闭。
        """
        key = self._history_key(pond_id, device_id, sensor_type)
        count = self._recovery_counters.get(key, 0) + 1

        if count < settings.RECOVERY_CONSECUTIVE_POINTS:
            self._recovery_counters[key] = count
            return

        self._recovery_counters.pop(key, None)

        # 延迟导入：避免数据接收路径与调度器模块产生不必要的耦合
        from app.tools.alert_tracker import resolve_sensor_alerts

        try:
            resolve_sensor_alerts(pond_id, device_id, sensor_type)
        except Exception as e:
            logger.warning(f"告警恢复处理失败: {e}")

    def check_alert(self, pond_id: str, device_id: str, sensor_type: str, value: float, ts_ns: int) -> Optional:
        """检测传感器值是否触发告警

        Args:
            pond_id: 鱼塘ID
            device_id: 设备ID
            sensor_type: 传感器类型
            value: 传感器数值
            ts_ns: 时间戳（纳秒）

        Returns:
            Optional[AlertEvent]: 告警事件对象，未触发告警时返回 None

        Note:
            调用 detector.check() 进行阈值检测，检测结果写入 MySQL alerts 表。
            同一告警在重复抑制窗口内只入库一次，但依然返回事件供决策链路使用，
            避免持续异常每 5 秒写一条记录把告警表刷爆。

            指标正常时走恢复计数：连续 RECOVERY_CONSECUTIVE_POINTS 个采样点
            都没有告警，才把 alert_duration 中的活跃记录置为 resolved，
            避免指标在阈值附近抖动导致告警反复开关。
        """
        alert = check(pond_id, device_id, sensor_type, value, self._get_history(pond_id, device_id, sensor_type))
        if not alert:
            self._handle_recovery(pond_id, device_id, sensor_type)
            return None

        # 出现告警说明指标仍异常，重置该指标的连续正常计数
        self._recovery_counters.pop(self._history_key(pond_id, device_id, sensor_type), None)

        ts_ms = ts_ns / 1000000
        ts_dt = datetime.fromtimestamp(ts_ms / 1000)
        ts_str = ts_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        # 趋势类告警用更长的抑制窗口，避免正常波动持续刷屏
        is_trend_alert = "_trend_" in alert.alert_type
        suppress_seconds = (
            settings.TREND_ALERT_REPEAT_WINDOW_SECONDS if is_trend_alert
            else settings.ALERT_REPEAT_WINDOW_SECONDS
        )
        window_start = (
            ts_dt - timedelta(seconds=suppress_seconds)
        ).strftime('%Y-%m-%d %H:%M:%S')

        recent = execute_mysql(
            """
            SELECT id FROM alerts
            WHERE pond_id = %s AND device_id = %s AND alert_type = %s AND ts >= %s
            LIMIT 1
            """,
            (pond_id, device_id, alert.alert_type, window_start),
        )

        if recent:
            logger.debug(
                f"告警抑制重复入库: {alert.message} (severity={alert.severity})"
            )
            return alert

        execute_mysql(
            """
            INSERT INTO alerts
            (ts, pond_id, device_id, alert_type, alert_value, threshold_value, message, is_read)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
            """,
            (ts_str, pond_id, device_id, alert.alert_type,
             alert.value, alert.threshold, alert.message[:500]),
        )
        logger.warning(
            f"触发告警: {alert.message} (severity={alert.severity} trend={alert.trend})"
        )

        return alert

    def process_device_status(self, data: dict):
        """处理设备上下线状态变更

        Args:
            data: 包含设备状态信息的字典

        Note:
            status=1 表示上线，status=0 表示下线
            login_type=7 为 MQTT 登录类型（默认值）
            写入 MySQL device_status 表，(device_id, ts) 联合主键避免同秒覆盖
        """
        dev_id = str(data.get('dev_name', data.get('dev_id', 'unknown')))
        status = data.get('status', 0)
        login_type = data.get('login_type', 7)
        ts_ms = data.get('at', data.get('time', int(datetime.now().timestamp() * 1000)))
        ts_str = datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        sql = """
            INSERT INTO fishery.device_status (ts, device_id, status, login_type)
            VALUES (%s, %s, %s, %s)
        """
        execute_sql(sql, (ts_str, dev_id, int(status), int(login_type)))
        logger.info(f"设备{'上线' if status == 1 else '下线'}: {dev_id}")