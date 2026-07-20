"""Agent 问答接口：智能对话 + 决策记录查询"""

from fastapi import APIRouter, Query

from app.core.db import execute_sql
from app.models.schemas import ChatRequest, ChatResponse
from app.agents.core import chat as agent_chat

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def agent_chat_endpoint(req: ChatRequest):
    try:
        reply = agent_chat(req.message)
        return ChatResponse(success=True, reply=reply)
    except Exception as e:
        return ChatResponse(success=False, reply=f"Agent 调用失败: {str(e)}")


@router.get("/decisions")
async def get_agent_decisions(
    pond_id: str = Query(None, description="鱼塘ID"),
    limit: int = Query(20, description="返回条数")
):
    """获取 Agent 决策记录

    TDengine 限制：时序表 JOIN 必须包含主时间戳等值条件
    因此采用两步查询方案：
    1. 查询 device_commands 获取指令记录
    2. 查询 alert_duration 获取活跃告警信息
    3. 在应用层进行关联

    Args:
        pond_id: 可选，按鱼塘ID筛选
        limit: 返回条数限制
    """
    try:
        where_clause = ""
        if pond_id:
            where_clause = f"WHERE pond_id = '{pond_id}'"

        commands_sql = f"""
            SELECT ts, pond_id, device_id, command_type, trigger_source,
                   trigger_alert_id, params, status, executed_at, result
            FROM fishery.device_commands
            {where_clause}
            ORDER BY ts DESC
            LIMIT {limit}
        """
        commands_result = execute_sql(commands_sql)

        commands = []
        if commands_result.get("code") == 0 and commands_result.get("data"):
            columns = [col[0] for col in commands_result.get("column_meta", [])]
            for row in commands_result["data"]:
                record = dict(zip(columns, row))
                for key in ("result", "params"):
                    if record.get(key) and isinstance(record[key], str):
                        record[key] = record[key].rstrip("\x00").strip()
                commands.append(record)

        alert_where = "WHERE status IN ('active', 'escalated')"
        if pond_id:
            alert_where += f" AND pond_id = '{pond_id}'"

        alerts_sql = f"""
            SELECT pond_id, alert_type, alert_value, threshold_value, agent_decision
            FROM fishery.alert_duration
            {alert_where}
        """
        alerts_result = execute_sql(alerts_sql)

        alert_map = {}
        if alerts_result.get("code") == 0 and alerts_result.get("data"):
            columns = [col[0] for col in alerts_result.get("column_meta", [])]
            for row in alerts_result["data"]:
                record = dict(zip(columns, row))
                if record.get("agent_decision") and isinstance(record["agent_decision"], str):
                    record["agent_decision"] = record["agent_decision"].rstrip("\x00").strip()
                key = f"{record.get('pond_id', '')}_{record.get('alert_type', '')}"
                alert_map[key] = record

        data_list = []
        for cmd in commands:
            entry = cmd.copy()
            trigger_alert_id = cmd.get("trigger_alert_id", "")
            pond = cmd.get("pond_id", "")
            alert_key = f"{pond}_{trigger_alert_id}"
            if alert_key in alert_map:
                alert = alert_map[alert_key]
                entry["alert_type"] = alert.get("alert_type")
                entry["alert_value"] = alert.get("alert_value")
                entry["threshold_value"] = alert.get("threshold_value")
                entry["agent_decision"] = alert.get("agent_decision")
            else:
                entry["alert_type"] = None
                entry["alert_value"] = None
                entry["threshold_value"] = None
                entry["agent_decision"] = None
            data_list.append(entry)

        return {"success": True, "data": data_list, "count": len(data_list)}
    except Exception as e:
        print(f"查询决策记录失败: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "data": [], "message": str(e)}