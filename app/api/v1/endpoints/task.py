"""人工工单接口：查询工单、标记处理中、标记已处理"""

from fastapi import APIRouter, Query

from app.core.db import execute_mysql
from app.models.schemas import ResolveTaskRequest

router = APIRouter()


@router.get("")
async def get_manual_tasks(
    status: str = Query(None, description="工单状态"),
    pond_id: str = Query(None, description="鱼塘ID")
):
    try:
        conditions = []
        params = []

        if status:
            conditions.append("status = %s")
            params.append(status)
        if pond_id:
            conditions.append("pond_id = %s")
            params.append(pond_id)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT id, pond_id, alert_type, alert_value, threshold_value,
                   started_at, escalated_at, status, handler, remark, created_at
            FROM manual_tasks
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT 50
        """
        rows = execute_mysql(sql, tuple(params) if params else None)
        return {"success": True, "data": rows or [], "count": len(rows) if rows else 0}
    except Exception as e:
        print(f"查询工单失败: {e}")
        return {"success": False, "data": [], "message": str(e)}


@router.post("/{task_id}/resolve")
async def resolve_manual_task(task_id: int, req: ResolveTaskRequest):
    try:
        sql = """
            UPDATE manual_tasks
            SET status = 'resolved', handler = %s, remark = %s
            WHERE id = %s AND status != 'resolved'
        """
        execute_mysql(sql, (req.handler, req.remark, task_id))
        return {"success": True, "message": f"工单 {task_id} 已标记为已处理"}
    except Exception as e:
        print(f"处理工单失败: {e}")
        return {"success": False, "message": str(e)}


@router.post("/{task_id}/process")
async def process_manual_task(task_id: int, req: ResolveTaskRequest):
    try:
        sql = """
            UPDATE manual_tasks
            SET status = 'processing', handler = %s, remark = %s
            WHERE id = %s AND status = 'pending'
        """
        execute_mysql(sql, (req.handler, req.remark, task_id))
        return {"success": True, "message": f"工单 {task_id} 已标记为处理中"}
    except Exception as e:
        print(f"处理工单失败: {e}")
        return {"success": False, "message": str(e)}