"""数据处理服务：数据清洗、缺失值填充、滑动窗口、入库、告警触发

核心职责：
1. 接收传感器原始数据，进行合法性校验和清洗
2. 使用滑动窗口维护最近的传感器历史值
3. 对缺失数据进行均值填充
4. 将清洗后的数据写入 TDengine
5. 触发异常检测，critical 级别告警自动启动 Agent 决策
"""

import json
from collections import deque
from datetime import datetime
from typing import Optional

from app.core.config import settings
from app.core.db import execute_sql, execute_mysql
from app.tools.detector import check


class DataProcessor:
    """传感器数据处理核心类

    管理传感器数据的完整生命周期：清洗 → 填充 → 入库 → 告警检测
    使用滑动窗口维护最近 10 条历史数据，用于波动率检测和缺失值填充
    """

    def __init__(self):
        """初始化数据处理器

        _sensor_history: 滑动窗口字典，key 格式为 "{pond_id}:{device_id}:{sensor_type}"
                         value 为 deque，最多保存最近 10 条 (时间戳ms, 值) 元组
        """
        self._sensor_history: dict[str, deque] = {}

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
            print(f"[CLEAN] 异常值丢弃: pond={pond_id} dev={device_id} {sensor_type}={value} (范围 {min_val}~{max_val})")
            return None

        history = self._get_history(pond_id, device_id, sensor_type)
        if history:
            prev_val = history[-1][1]
            if prev_val != 0:
                change_rate = abs(value - prev_val) / abs(prev_val)
                if change_rate > 0.5:
                    print(f"[CLEAN] 波动率异常: pond={pond_id} dev={device_id} {sensor_type} "
                          f"{prev_val:.2f} → {value:.2f} (变化率 {change_rate:.1%})")

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
        print(f"[FILL] 缺失值填充: pond={pond_id} dev={device_id} {sensor_type} → {avg:.2f} (窗口={len(recent)})")
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

    async def process_one_message(self, data: dict):
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
            await self.process_data_point(data)
        elif msg_type == 2 or notify_type == 'deviceStatus':
            await self.process_device_status(data)
        else:
            print(f"未知消息类型: type={msg_type}, notifyType={notify_type}, messageType={message_type}")

    async def process_data_point(self, data: dict):
        """处理单个传感器数据点（核心流程）

        完整处理流程：
        1. 提取设备ID、产品ID、参数数据
        2. 查询设备绑定的鱼塘ID
        3. 遍历传感器参数，进行清洗
        4. 对缺失的指标进行均值填充
        5. 更新滑动窗口历史
        6. 组装 SQL 写入 TDengine
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
            print(f"设备未绑定: {device_id}")
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
            print(f"无有效传感器数据: {device_id}")
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

        table_name = f"sensor_data_{pond_id}_{device_id}"
        t = collected.get('temperature', 'NULL')
        p = collected.get('ph_value', 'NULL')
        do = collected.get('dissolved_oxygen', 'NULL')
        nh = collected.get('ammonia_nitrogen', 'NULL')
        no = collected.get('nitrite', 'NULL')
        ts_v = collected.get('ts_value', 'NULL')
        wl = collected.get('water_level', 'NULL')

        sql = f"""
            INSERT INTO fishery.{table_name}
            USING fishery.sensor_data TAGS ('{pond_id}', '{device_id}', '{product_id}')
            VALUES ('{ts_str}', {t}, {p}, {do}, {nh}, {no}, {ts_v}, {wl})
        """
        execute_sql(sql)
        print(f"[DATA] pond={pond_id} dev={device_id} | temp={t} pH={p} DO={do} NH3={nh} NO2={no} TS={ts_v} WL={wl}")

        from app.tools.rules import check_emergency_rules
        emergency_result = check_emergency_rules(pond_id, device_id, collected)
        if emergency_result:
            print(f"[EMERGENCY] {emergency_result.reason}")
            from app.tools.executor import execute
            execute(pond_id, device_id, {
                "action": emergency_result.action,
                "params": emergency_result.params,
                "reason": emergency_result.reason
            }, f"{emergency_result.action}_emergency")
        else:
            for field_name, value in collected.items():
                alert = await self.check_alert(pond_id, device_id, field_name, value, ts_ns)
                if alert and alert.severity == "critical":
                    from app.services.agent_pipeline import agent_decision_pipeline
                    import asyncio
                    asyncio.create_task(agent_decision_pipeline(alert))

    async def check_alert(self, pond_id: str, device_id: str, sensor_type: str, value: float, ts_ns: int) -> Optional:
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
            调用 detector.check() 进行阈值检测，检测结果写入 alerts 表
        """
        alert = check(pond_id, device_id, sensor_type, value, self._get_history(pond_id, device_id, sensor_type))
        if not alert:
            return

        ts_ms = ts_ns / 1000000
        ts_str = datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            INSERT INTO fishery.alerts (ts, pond_id, device_id, alert_type, alert_value, threshold_value, message, is_read)
            VALUES ('{ts_str}', '{pond_id}', '{device_id}', '{alert.alert_type}', {alert.value}, {alert.threshold}, '{alert.message}', false)
        """
        execute_sql(sql)
        print(f"[ALERT] {alert.message} (severity={alert.severity} trend={alert.trend})")

        return alert

    async def process_device_status(self, data: dict):
        """处理设备上下线状态变更

        Args:
            data: 包含设备状态信息的字典

        Note:
            status=1 表示上线，status=0 表示下线
            login_type=7 为 MQTT 登录类型（默认值）
        """
        dev_id = str(data.get('dev_name', data.get('dev_id', 'unknown')))
        status = data.get('status', 0)
        login_type = data.get('login_type', 7)
        ts_ms = data.get('at', data.get('time', int(datetime.now().timestamp() * 1000)))
        ts_str = datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')

        sql = f"""
            INSERT INTO fishery.device_status (ts, device_id, status, login_type)
            VALUES ('{ts_str}', '{dev_id}', {status}, {login_type})
        """
        execute_sql(sql)
        print(f"设备{'上线' if status == 1 else '下线'}: {dev_id}")