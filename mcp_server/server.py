"""渔业养殖 MCP Server —— 知识库检索与水质数据查询（只读）

设计约束（与主服务的安全架构保持一致）：
    本 server **只暴露只读工具**，不含任何设备控制入口。主服务的核心原则是
    「LLM 只提议、Python 统一执行」，因此经 MCP 接入的外部客户端同样无法
    直接操控增氧机 / 水泵 / 投饵机，只能拿到数据并给出建议。

传输：stdio（供 Claude Desktop / Cline / Inspector 等本地客户端接入）

启动方式：
    cd Backend
    python -m mcp_server

客户端配置示例（Claude Desktop 的 claude_desktop_config.json）：
    {
      "mcpServers": {
        "fishery": {
          "command": "<python 解释器绝对路径>",
          "args": ["-m", "mcp_server"],
          "cwd": "<项目 Backend 目录绝对路径>"
        }
      }
    }

调试方式：
    npx @modelcontextprotocol/inspector python -m mcp_server

注意：stdio 传输独占标准输出（JSON-RPC 帧），因此所有日志一律写入 stderr；
工具内部对异常做兜底，任何失败都以 ``{"ok": false, "error": ...}`` 返回，
不向协议层抛异常（否则客户端会收到 -32603 内部错误并断开）。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("fishery.mcp")

SensorMetric = Literal[
    "temperature", "ph_value", "dissolved_oxygen",
    "ammonia_nitrogen", "nitrite", "ts_value", "water_level",
]
KnowledgeCategory = Literal["water_quality", "diseases", "breeding"]

server = MCPServer(
    name="fishery",
    title="鳜鱼养殖智能监控",
    version="1.0.0",
    instructions=(
        "提供鳜鱼养殖知识库检索、水质指标查询、告警与设备状态查询。"
        "所有工具均为只读，无法控制增氧机、水泵、投饵机等设备。"
        "拿到数据后可以给出处置建议，但必须提示由人工确认执行。"
        "水质判断请以 get_water_quality_standards 返回的阈值为准，不要臆测数值。"
    ),
)


def _ok(payload: Any) -> str:
    return json.dumps({"ok": True, "data": payload}, ensure_ascii=False, default=str)


def _fail(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


# --------------------------------------------------------------------- 工具定义

@server.tool(
    description="检索鳜鱼养殖知识库，覆盖水质调控、病害防治、苗种培育。返回来源文档、标题与正文片段",
    structured_output=False,
)
def search_knowledge(query: str, category: KnowledgeCategory | None = None) -> str:
    """检索知识库

    Args:
        query: 检索关键词，如 "溶解氧偏低怎么处理"
        category: 可选分类过滤；不传则在全部知识中检索
    """
    try:
        from app.agent.tools import _search_knowledge

        results = _search_knowledge(query, category)
        if not results:
            return _ok({"hits": [], "note": "未检索到相关内容，可更换关键词重试"})
        return _ok({"hits": results, "count": len(results)})
    except Exception as exc:  # noqa: BLE001 工具失败必须降级为结构化错误
        logger.exception("[MCP] search_knowledge 失败")
        return _fail(f"知识库检索失败: {exc}")


@server.tool(
    description="查询指定鱼塘最新的 7 项水质指标：温度、pH、溶解氧、氨氮、亚硝酸盐、浊度、水位",
    structured_output=False,
)
def get_sensor_snapshot(pond_id: str, device_id: str) -> str:
    """查询最新水质快照

    Args:
        pond_id: 鱼塘 ID
        device_id: 采集设备 ID
    """
    try:
        from app.agent.tools import _get_sensors

        data = _get_sensors(pond_id, device_id)
        if not data:
            return _ok({"sensors": None, "note": f"鱼塘 {pond_id} / 设备 {device_id} 暂无采样数据"})
        return _ok({"sensors": data})
    except Exception as exc:  # noqa: BLE001
        logger.exception("[MCP] get_sensor_snapshot 失败")
        return _fail(f"水质快照查询失败: {exc}")


@server.tool(
    description="查询某项水质指标最近 N 分钟的历史采样点，用于判断变化趋势（升/降/骤变）",
    structured_output=False,
)
def get_sensor_history(
    pond_id: str,
    device_id: str,
    metric: SensorMetric,
    minutes: int = 30,
) -> str:
    """查询指标历史

    Args:
        pond_id: 鱼塘 ID
        device_id: 采集设备 ID
        metric: 指标列名，必须是 7 项合法指标之一
        minutes: 时间窗口（分钟），1~120
    """
    try:
        from app.agent.tools import _get_history

        rows = _get_history(pond_id, device_id, metric, minutes)
        if not rows:
            return _ok({"metric": metric, "points": [], "note": "该时间窗内无采样点"})
        return _ok({"metric": metric, "minutes": minutes, "points": rows, "count": len(rows)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("[MCP] get_sensor_history 失败")
        return _fail(f"历史数据查询失败: {exc}")


@server.tool(
    description="查询鱼塘内增氧机、水泵、投饵机的当前运行状态",
    structured_output=False,
)
def get_device_status(pond_id: str) -> str:
    """查询设备状态

    Args:
        pond_id: 鱼塘 ID
    """
    try:
        from app.agent.tools import _get_device_status

        data = _get_device_status(pond_id)
        return _ok({"devices": data or [], "count": len(data or [])})
    except Exception as exc:  # noqa: BLE001
        logger.exception("[MCP] get_device_status 失败")
        return _fail(f"设备状态查询失败: {exc}")


@server.tool(
    description="查询当前活跃告警（指标持续性越界），可指定鱼塘；不传则返回全部鱼塘",
    structured_output=False,
)
def get_active_alerts(pond_id: str | None = None) -> str:
    """查询活跃告警

    Args:
        pond_id: 可选，鱼塘 ID
    """
    try:
        from app.api.v1.alert import _query_alerts_by_status

        payload = _query_alerts_by_status("active", pond_id)
        if not payload.get("success"):
            return _fail(f"告警查询失败: {payload.get('message', 'unknown')}")
        return _ok({"alerts": payload.get("data", []), "count": payload.get("count", 0)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("[MCP] get_active_alerts 失败")
        return _fail(f"告警查询失败: {exc}")


@server.tool(
    description="获取水质告警阈值与紧急阈值标准（不依赖数据库，可先用它确认判定口径）",
    structured_output=False,
)
def get_water_quality_standards() -> str:
    """获取水质判定阈值"""
    try:
        from app.core.config import settings

        return _ok(
            {
                "alert_thresholds": settings.ALERT_THRESHOLDS,
                "emergency_thresholds": settings.EMERGENCY_THRESHOLDS,
                "unit_note": "各指标单位与养殖档案一致，pH 为无量纲",
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[MCP] get_water_quality_standards 失败")
        return _fail(f"阈值读取失败: {exc}")


def main() -> None:
    """以 stdio 传输启动 MCP server（阻塞）"""
    logger.warning("[MCP] fishery server 启动，transport=stdio")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
