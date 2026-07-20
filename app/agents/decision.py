"""Agent 自主决策 — 水质异常时调用 LLM 分析并输出结构化决策

核心功能：
1. 接收告警上下文信息（告警类型、当前值、阈值、历史趋势等）
2. 调用 LLM（GLM-4.5）进行智能分析和决策，支持重试机制
3. LLM 调用失败/超时时，使用规则引擎作为兜底方案
4. 解析 LLM 返回的 JSON 格式决策
5. 处理解析失败的兜底逻辑

决策系统提示词（DECISION_SYSTEM_PROMPT）定义了：
- 可用动作枚举（start_aerator, stop_aerator, start_pump, stop_pump, alert_human, no_action）
- 决策规则（溶解氧 < 3mg/L → 开增氧机等）
- 输出格式要求（严格 JSON）

调用时机：当 detector 检测到 critical 级别告警时，由 agent_pipeline 调用

重试策略：
- 最大重试次数：2 次
- 重试间隔：指数退避（1s, 2s）
- 超时时间：30 秒
- 所有重试失败时，使用规则引擎兜底
"""

import json
import re
import time
from threading import Lock
from openai import OpenAI, APITimeoutError, APIConnectionError, APIStatusError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_result, RetryError

from app.agents.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from app.agents.prompt_decision import DECISION_SYSTEM_PROMPT

_llm_lock = Lock()
_last_request_time = 0
_MIN_REQUEST_INTERVAL = 3


def _get_client():
    if not LLM_API_KEY:
        raise ValueError("LLM_API_KEY 未配置，请在 .env 文件中设置")
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def _rule_based_decision(alert_context: dict) -> dict:
    """规则引擎兜底决策：当 LLM 调用失败时使用简单规则生成决策

    规则优先级：
    1. 溶解氧 < 2mg/L → 立即开增氧机 + 报人工（紧急情况）
    2. 溶解氧 < 3mg/L → 开增氧机
    3. pH < 6.5 或 pH > 9.0 → 开水泵换水 + 报人工
    4. 氨氮 > 0.5mg/L → 开水泵换水 + 报人工
    5. 水温 > 33℃ → 开水泵换水
    6. 其他异常 → 报人工
    """
    alert_type = alert_context.get("alert_type", "")
    sensor_type = alert_context.get("sensor_type", "")
    current_value = alert_context.get("current_value", 0)
    threshold = alert_context.get("threshold", 0)
    pond_id = alert_context.get("pond_id", "")

    if sensor_type == "dissolved_oxygen":
        if current_value < 2.0:
            return {
                "analysis": f"溶解氧极低({current_value}mg/L)，属于紧急情况，必须立即处理",
                "decision": {
                    "action": "start_aerator",
                    "params": {"duration": 60, "power": "high"},
                    "reason": "溶解氧 < 2mg/L，立即开启增氧机并通知人工"
                },
                "follow_up": "开启增氧机后持续监测，同时通知人工现场确认"
            }
        elif current_value < 3.0:
            return {
                "analysis": f"溶解氧偏低({current_value}mg/L < {threshold}mg/L)，需要增氧",
                "decision": {
                    "action": "start_aerator",
                    "params": {"duration": 30},
                    "reason": "溶解氧低于安全阈值，开启增氧机提升水中溶氧量"
                },
                "follow_up": "30分钟后复测溶解氧，如未回升需考虑换水"
            }

    elif sensor_type == "ph_value":
        if current_value < 6.5:
            return {
                "analysis": f"pH值过低({current_value} < {threshold})，水质偏酸",
                "decision": {
                    "action": "alert_human",
                    "params": {"priority": "high"},
                    "reason": "pH值严重偏低，需人工检查并调整水质"
                },
                "follow_up": "建议换水20%并投加适量生石灰调节pH"
            }
        elif current_value > 9.0:
            return {
                "analysis": f"pH值过高({current_value} > 8.5)，水质偏碱",
                "decision": {
                    "action": "alert_human",
                    "params": {"priority": "high"},
                    "reason": "pH值严重偏高，需人工检查并调整水质"
                },
                "follow_up": "建议换水20%并投加适量有机酸调节pH"
            }

    elif sensor_type == "ammonia_nitrogen":
        if current_value > 0.5:
            return {
                "analysis": f"氨氮超标({current_value}mg/L > {threshold}mg/L)",
                "decision": {
                    "action": "start_pump",
                    "params": {"duration": 30},
                    "reason": "氨氮浓度过高，开启水泵换水降低浓度"
                },
                "follow_up": "换水后复测氨氮，如仍超标需减少投喂量"
            }

    elif sensor_type == "temperature":
        if current_value > 33.0:
            return {
                "analysis": f"水温过高({current_value}℃)，超过安全上限",
                "decision": {
                    "action": "start_pump",
                    "params": {"duration": 60},
                    "reason": "水温过高会导致溶解氧下降，开启水泵换水降温"
                },
                "follow_up": "持续监测水温变化，必要时增加换水频率"
            }

    return {
        "analysis": f"{alert_type}：{current_value}，LLM决策不可用，使用规则兜底",
        "decision": {
            "action": "alert_human",
            "params": {"priority": "medium", "reason": f"告警类型: {alert_type}, 值: {current_value}, 阈值: {threshold}"},
            "reason": "LLM调用失败，触发人工审核"
        },
        "follow_up": "请人工检查水质状况并决定是否需要干预"
    }


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=3, max=10),
    retry=retry_if_exception_type((APITimeoutError, APIConnectionError, APIStatusError)),
    reraise=False
)
def _call_llm_with_retry(client: OpenAI, model: str, messages: list):
    """带重试的 LLM 调用（包含速率限制）"""
    global _last_request_time
    
    with _llm_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            sleep_time = _MIN_REQUEST_INTERVAL - elapsed
            time.sleep(sleep_time)
        _last_request_time = time.time()
    
    return client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=1000,
        timeout=30,
    )


def decide(alert_context: dict) -> dict:
    """调用 LLM 进行自主决策，支持重试和规则兜底

    执行流程：
    1. 将告警上下文序列化为 JSON 字符串
    2. 构建 LLM 对话消息（system prompt + user message）
    3. 调用 LLM API 获取决策响应（带重试）
    4. 所有重试失败时，使用规则引擎兜底
    5. 解析响应内容（支持直接 JSON 和 markdown 代码块包裹两种格式）
    6. 解析失败时返回兜底决策（alert_human）

    Args:
        alert_context: 告警上下文字典，包含以下字段：
            - alert_type: 告警类型（如 dissolved_oxygen_low）
            - sensor_type: 传感器类型（如 dissolved_oxygen）
            - current_value: 当前传感器数值
            - threshold: 触发告警的阈值
            - severity: 严重级别（critical/warning）
            - trend: 变化趋势（rising/falling/stable）
            - pond_id: 鱼塘ID
            - message: 告警消息描述
            - history_10min: 最近10分钟的历史数据列表

    Returns:
        dict: 决策结果，包含以下字段：
            - analysis: 对当前水质状况的简要分析（字符串）
            - decision: 决策详情（字典）
              - action: 动作类型（start_aerator/stop_aerator/start_pump/stop_pump/start_feeder/stop_feeder/alert_human/no_action）
              - params: 动作参数（字典，如 {"duration": 30}）
              - reason: 执行该动作的具体原因（字符串）
            - follow_up: 后续建议（字符串）
    """
    user_msg = json.dumps(alert_context, ensure_ascii=False, indent=2)

    client = _get_client()
    messages = [
        {"role": "system", "content": DECISION_SYSTEM_PROMPT},
        {"role": "user", "content": f"当前水质异常，请分析并决策：\n{user_msg}"},
    ]

    try:
        response = _call_llm_with_retry(client, LLM_MODEL, messages)
    except RetryError as e:
        print(f"[AGENT] LLM 调用重试失败: {e}")
        print(f"[AGENT] 切换到规则引擎兜底决策")
        return _rule_based_decision(alert_context)
    except (APITimeoutError, APIConnectionError, APIStatusError) as e:
        print(f"[AGENT] LLM 调用失败: {e}")
        print(f"[AGENT] 切换到规则引擎兜底决策")
        return _rule_based_decision(alert_context)
    except Exception as e:
        print(f"[AGENT] LLM 调用异常: {e}")
        return _rule_based_decision(alert_context)

    content = response.choices[0].message.content

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{[\s\S]*\}', content)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    print(f"[AGENT] 决策 JSON 解析失败，原始输出: {content}")
    print(f"[AGENT] 切换到规则引擎兜底决策")
    return _rule_based_decision(alert_context)