"""
数据模拟器 — 开发阶段替代硬件，定时生成模拟水质数据并上报。

用法:
    python simulator.py                          # 默认：pond1, 5秒间隔
    python simulator.py --pond-id pond2          # 指定鱼塘
    python simulator.py --device-id dev_001      # 指定设备
    python simulator.py --interval 10            # 10秒间隔
    python simulator.py --anomaly-rate 0.1       # 10% 异常注入概率
"""

import asyncio
import argparse
import math
import random
from datetime import datetime

import aiohttp

# 默认配置
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
SIMULATOR_ENDPOINT = "/api/sensor-data/simulator/data"

# 各指标基础值、振幅、相位（正弦波参数）
# 每个指标独立正弦波，相位不同避免同步波动
SENSOR_CONFIG = {
    "temperature":       {"base": 25.5, "amplitude": 2.0,  "phase": 0.0},
    "ph_value":          {"base": 7.2,  "amplitude": 0.3,  "phase": 1.57},
    "dissolved_oxygen":  {"base": 6.5,  "amplitude": 1.5,  "phase": 3.14},
    "ammonia_nitrogen":  {"base": 0.15, "amplitude": 0.05, "phase": 0.78},
    "nitrite":           {"base": 0.05, "amplitude": 0.03, "phase": 2.35},
    "ts_value":          {"base": 45.0, "amplitude": 10.0, "phase": 4.71},
    "water_level":       {"base": 1.8,  "amplitude": 0.1,  "phase": 1.0},
}

# 异常注入场景：模拟真实可能出现的异常情况
ANOMALY_SCENARIOS = [
    # (指标名, 异常值, 概率权重, 描述)
    ("dissolved_oxygen", 2.5, 30, "溶解氧过低（凌晨缺氧）"),
    ("dissolved_oxygen", 1.8, 10, "溶解氧严重过低（可能死鱼）"),
    ("ammonia_nitrogen", 0.55, 20, "氨氮超标"),
    ("ph_value", 5.8, 15, "pH 过低（酸雨影响）"),
    ("ph_value", 9.2, 10, "pH 过高（藻类爆发）"),
    ("temperature", 33.5, 15, "水温过高（夏季高温）"),
]


class WaterQualitySimulator:
    """水质数据模拟器"""

    def __init__(self, pond_id: str, device_id: str, base_url: str,
                 interval: float = 5.0, anomaly_rate: float = 0.05):
        self.pond_id = pond_id
        self.device_id = device_id
        self.base_url = base_url
        self.interval = interval
        self.anomaly_rate = anomaly_rate
        self.tick = 0  # 模拟周期计数，用于正弦波相位推进
        self.session: aiohttp.ClientSession | None = None

    def _time_of_day_factor(self) -> float:
        """根据当前时间计算时段影响因子（0~1 之间波动）"""
        hour = datetime.now().hour + datetime.now().minute / 60.0
        # 一天 24 小时为一个周期，凌晨 4 点最低，下午 14 点最高
        return 0.5 + 0.5 * math.sin((hour - 4) / 24 * 2 * math.pi)

    def _generate_value(self, sensor_type: str) -> float:
        """生成单个指标的值：正弦曲线 + 随机噪声 + 时段影响"""
        config = SENSOR_CONFIG[sensor_type]
        base = config["base"]
        amplitude = config["amplitude"]
        phase = config["phase"]

        # 正弦波：周期 72 个 tick（6 分钟一个完整周期）
        sine = amplitude * math.sin(2 * math.pi * self.tick / 72 + phase)

        # 随机噪声：±5% 的随机扰动
        noise = random.uniform(-0.05, 0.05) * base

        value = base + sine + noise

        # 时段影响：溶解氧与水温
        tod = self._time_of_day_factor()
        if sensor_type == "dissolved_oxygen":
            # 凌晨 DO 低，午后 DO 高（与水温趋势相反）
            value -= 0.5 * (1 - tod)
        elif sensor_type == "temperature":
            # 午后水温高，凌晨低
            value += 1.0 * tod

        # 溶解氧与水温反相关
        # 这里不直接引用 temperature 值，避免耦合，用时段因子近似
        if sensor_type == "dissolved_oxygen":
            value -= 0.3 * tod

        return round(value, 2)

    def _should_inject_anomaly(self) -> tuple[str, float, str] | None:
        """判断是否注入异常，返回 (指标名, 异常值, 描述) 或 None"""
        if random.random() > self.anomaly_rate:
            return None

        # 按权重随机选择异常场景
        total_weight = sum(w for _, _, w, _ in ANOMALY_SCENARIOS)
        r = random.uniform(0, total_weight)
        cumulative = 0
        for sensor_type, value, weight, desc in ANOMALY_SCENARIOS:
            cumulative += weight
            if r <= cumulative:
                return sensor_type, value, desc
        return None

    def generate_data(self) -> dict:
        """生成一组完整的模拟水质数据"""
        data = {
            "device_id": self.device_id,
            "product_id": "simulator",
        }

        anomaly = self._should_inject_anomaly()

        for sensor_type in SENSOR_CONFIG:
            if anomaly and sensor_type == anomaly[0]:
                # 注入异常值
                value = anomaly[1]
                print(f"[SIMULATOR] ⚠ 异常注入: {anomaly[2]} | {sensor_type}={value}")
            else:
                value = self._generate_value(sensor_type)
            data[sensor_type] = value

        self.tick += 1
        return data

    async def report(self) -> None:
        """上报一次数据到后端"""
        if self.session is None:
            self.session = aiohttp.ClientSession()

        data = self.generate_data()
        url = f"{self.base_url}{SIMULATOR_ENDPOINT}"

        try:
            async with self.session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                result = await resp.json()
                ts = datetime.now().strftime("%H:%M:%S")
                if resp.status == 200:
                    print(f"[SIMULATOR] {ts} | 上报成功 | pond={self.pond_id} dev={self.device_id} "
                          f"DO={data.get('dissolved_oxygen')} T={data.get('temperature')} "
                          f"pH={data.get('ph_value')} NH3={data.get('ammonia_nitrogen')}")
                else:
                    print(f"[SIMULATOR] {ts} | 上报失败 HTTP {resp.status}: {result}")
        except aiohttp.ClientError as e:
            print(f"[SIMULATOR] {datetime.now().strftime('%H:%M:%S')} | 连接失败: {e}")
        except Exception as e:
            print(f"[SIMULATOR] {datetime.now().strftime('%H:%M:%S')} | 异常: {e}")

    async def run(self) -> None:
        """启动模拟器主循环"""
        print(f"[SIMULATOR] 启动: pond_id={self.pond_id} device_id={self.device_id} "
              f"interval={self.interval}s anomaly_rate={self.anomaly_rate:.0%}")
        print(f"[SIMULATOR] 后端地址: {self.base_url}{SIMULATOR_ENDPOINT}")

        try:
            while True:
                await self.report()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            print("[SIMULATOR] 收到停止信号，正在退出...")
        finally:
            if self.session:
                await self.session.close()


async def main():
    parser = argparse.ArgumentParser(description="Fishery 水质数据模拟器")
    parser.add_argument("--pond-id", default="pond1", help="鱼塘 ID（默认 pond1）")
    parser.add_argument("--device-id", default="sim_dev_001", help="设备 ID（默认 sim_dev_001）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"后端地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--interval", type=float, default=5.0, help="上报间隔秒数（默认 5）")
    parser.add_argument("--anomaly-rate", type=float, default=0.05, help="异常注入概率（默认 0.05）")
    args = parser.parse_args()

    simulator = WaterQualitySimulator(
        pond_id=args.pond_id,
        device_id=args.device_id,
        base_url=args.base_url,
        interval=args.interval,
        anomaly_rate=args.anomaly_rate,
    )
    await simulator.run()


if __name__ == "__main__":
    asyncio.run(main())