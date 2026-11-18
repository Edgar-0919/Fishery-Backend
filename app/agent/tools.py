"""Agent 工具集

**只有 4 个只读工具 + 1 个 propose_action，没有任何执行类工具。**
LLM 能自主决定查什么、查几轮、何时停，但永远碰不到设备控制。

工具 schema 使用标准 OpenAI function 格式，直接作为原生 openai SDK 的
``tools`` 参数下发；问答、决策、Agent 三条链路共用同一套 LLM 客户端。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agent.proposals import ActionProposal, AgentState
from app.core.config import settings
from app.core.db import execute_sql, safe_identifier

logger = logging.getLogger(__name__)

SENSOR_METRICS = (
    "temperature", "ph_value", "dissolved_oxygen",
    "ammonia_nitrogen", "nitrite", "ts_value", "water_level",
)

TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_sensors",
            "description": "查询指定鱼塘最新的全部 7 项水质指标（温度、pH、溶解氧、氨氮、亚硝酸盐、浊度、水位）",
            "parameters": {
                "type": "object",
                "properties": {
                    "pond_id": {"type": "string", "description": "鱼塘ID"},
                    "device_id": {"type": "string", "description": "采集设备ID"},
                },
                "required": ["pond_id", "device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sensor_history",
            "description": "查询某项水质指标最近 N 分钟的历史采样点，用于判断趋势",
            "parameters": {
                "type": "object",
                "properties": {
                    "pond_id": {"type": "string", "description": "鱼塘ID"},
                    "device_id": {"type": "string", "description": "采集设备ID"},
                    "metric": {
                        "type": "string",
                        "enum": list(SENSOR_METRICS),
                        "description": "指标列名，必须是 7 个合法指标之一",
                    },
                    "minutes": {"type": "integer", "description": "时间窗口（分钟），1~120"},
                },
                "required": ["pond_id", "device_id", "metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_status",
            "description": "查询鱼塘内增氧机/水泵/投饵机的当前运行状态",
            "parameters": {
                "type": "object",
                "properties": {"pond_id": {"type": "string", "description": "鱼塘ID"}},
                "required": ["pond_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "检索鳜鱼养殖知识库，获取水质调控、病害防治、养殖技术等处理方案。信息不足时可换关键词重试",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词，如'溶解氧偏低应急处理'"},
                    "category": {
                        "type": "string",
                        "enum": ["water_quality", "diseases", "breeding", "cases"],
                        "description": "知识分类（可选）",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": "提出一个待执行的处置动作。本工具只记录提议，不会真正控制设备；系统会做安全校验后决定是否执行",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "start_aerator", "stop_aerator", "start_pump", "stop_pump",
                            "start_feeder", "stop_feeder", "alert_human", "no_action",
                        ],
                        "description": "处置动作，必须是枚举值之一",
                    },
                    "params": {"type": "object", "description": "动作参数，如 duration(分钟,1~120)、power(low/medium/high)"},
                    "reason": {"type": "string", "description": "决策依据"},
                    "confidence": {"type": "number", "description": "置信度 0~1"},
                },
                "required": ["action", "reason"],
            },
        },
    },
]

TOOL_NAMES = {spec["function"]["name"] for spec in TOOL_SPECS}


def _ok(payload: Any) -> str:
    return json.dumps({"ok": True, "data": payload}, ensure_ascii=False, default=str)


def _fail(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _get_sensors(pond_id: str, device_id: str) -> Any:
    # 函数内延迟导入，避免与 services.agent_pipeline 产生顶层循环依赖
    from app.services.agent_pipeline import _get_all_sensors_latest

    return _get_all_sensors_latest(pond_id, device_id)


def _get_device_status(pond_id: str) -> Any:
    from app.services.agent_pipeline import _get_device_status

    return _get_device_status(pond_id)


def _get_history(pond_id: str, device_id: str, metric: str, minutes: int = 10) -> list[dict]:
    if metric not in SENSOR_METRICS:
        return []
    minutes = max(1, min(int(minutes or 10), 120))

    from datetime import datetime, timedelta

    end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
        SELECT ts, {safe_identifier(metric)} FROM fishery.sensor_data
        WHERE pond_id = %s AND device_id = %s
          AND ts >= %s AND ts <= %s
        ORDER BY ts DESC LIMIT 50
    """
    result = execute_sql(sql, (pond_id, device_id, start, end))
    if result.get("code") != 0 or not result.get("data"):
        return []
    return [{"ts": row[0], "value": row[1]} for row in result["data"]]


def _search_knowledge(query: str, category: str | None = None) -> list[dict]:
    try:
        from app.rag.retriever import retrieve

        results = retrieve(query, top_k=settings.RAG_TOP_K,
                           score_threshold=settings.RAG_SCORE_THRESHOLD)
        if category:
            results = [r for r in results if r.get("category") == category] or results
        return [
            {
                "source": r.get("source", ""),
                "title": r.get("title", ""),
                "score": r.get("score", 0),
                "content": str(r.get("content", ""))[:500],
            }
            for r in results
        ]
    except Exception as exc:
        logger.warning("[Agent] 知识检索失败（按无 RAG 继续）: %s", exc)
        return []


def execute_tool(name: str, args: dict, state: AgentState) -> str:
    """执行一个工具调用；任何失败都返回结构化错误，不向循环抛异常

    propose_action 只把校验通过的提议追加到内存状态，不做任何 IO。
    """
    try:
        if name not in TOOL_NAMES:
            return _fail(f"未知工具: {name}")

        if name == "get_sensors":
            return _ok(_get_sensors(args.get("pond_id", ""), args.get("device_id", "")))

        if name == "get_sensor_history":
            return _ok(_get_history(
                args.get("pond_id", ""), args.get("device_id", ""),
                args.get("metric", ""), args.get("minutes", 10),
            ))

        if name == "get_device_status":
            return _ok(_get_device_status(args.get("pond_id", "")))

        if name == "search_knowledge":
            query = str(args.get("query", "")).strip()
            results = _search_knowledge(query, args.get("category"))
            state.rag_queries.append({"query": query, "sources": [r["source"] for r in results]})
            return _ok(results)

        # propose_action：Pydantic 校验不通过时把错误回灌给 LLM，让它修正
        proposal = ActionProposal(**args)
        state.proposals.append(proposal)
        return _ok({"accepted": True, "index": len(state.proposals) - 1,
                    "action": proposal.action})
    except Exception as exc:
        logger.info("[Agent] 工具 %s 调用失败: %s", name, exc)
        return _fail(f"{name}: {exc}")
