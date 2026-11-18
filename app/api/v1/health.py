"""健康检查接口：存活探针、就绪探针、依赖详情

用途：
- /health/live   进程存活探针，不查依赖，避免数据库抖动导致服务被反复重启
- /health/ready  就绪探针，检查时序表与业务表是否可用，供负载均衡摘流
- /health        运维排查用，返回各依赖的明细

时序数据已从 TDengine 合并到 MySQL，两项检查都走同一个连接池，
分别验证 sensor_data 与 users 表是否就绪。
"""

import importlib.util
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.db import execute_mysql, execute_sql

router = APIRouter()


def _check_timeseries() -> dict:
    """检查时序数据表 sensor_data 是否就绪"""
    try:
        result = execute_sql("SELECT COUNT(*) AS total FROM fishery.sensor_data")
        if result.get("code") == 0:
            return {"ok": True, "detail": "sensor_data ready"}
        return {"ok": False, "detail": result.get("desc") or "query failed"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


def _check_mysql() -> dict:
    """检查 MySQL 连通性与 users 表是否就绪"""
    try:
        rows = execute_mysql("SELECT 1 AS ok")
        if not rows:
            return {"ok": False, "detail": "query returned no rows"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}

    try:
        tables = execute_mysql("SELECT COUNT(*) AS total FROM users")
        if not tables:
            return {"ok": False, "detail": "users 表不存在，请先执行 sql/fishery_mysql.sql"}
        return {"ok": True, "detail": f"users={tables[0].get('total', 0)}"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


def _collect_checks() -> dict:
    return {
        "timeseries": _check_timeseries(),
        "mysql": _check_mysql(),
    }


def _agent_status() -> dict:
    """Agent 运行态自检（信息性，不参与就绪判定）

    把「RAG 依赖缺失导致静默降级为无 RAG」「设备网关未接线」这类隐性状态
    暴露出来，便于运维在 /health 一眼看到，而不是只能在启动日志里翻 warning。
    """
    from app.core.config import settings

    missing = []
    for name in ("rank_bm25", "chromadb", "jieba"):
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)

    from app.agent.monitor import last_stats

    return {
        "llm_configured": bool(os.getenv("LLM_API_KEY") or settings.LLM_API_KEY),
        "rag_enabled": settings.RAG_ENABLED,
        "rag_dependencies": "available" if not missing
                            else f"missing: {', '.join(missing)}",
        # 最近一次后台扫描的 fallback 占比（读缓存，不查库）
        "decision_health": last_stats(),
        # gateway.py 未接线，executor 只登记指令不派发硬件
        "gateway": "unwired",
    }


@router.get("/health/live")
def liveness():
    """存活探针：进程能响应即为存活"""
    return {"status": "ok"}


@router.get("/health/ready")
def readiness():
    """就绪探针：所有依赖可用才返回 200"""
    checks = _collect_checks()
    ready = all(item["ok"] for item in checks.values())
    payload = {"status": "ready" if ready else "unavailable", "checks": checks}
    return JSONResponse(status_code=200 if ready else 503, content=payload)


@router.get("/health")
def health_detail():
    """依赖详情：便于排查具体哪个组件异常"""
    checks = _collect_checks()
    ready = all(item["ok"] for item in checks.values())
    return {
        "status": "ok" if ready else "degraded",
        "checks": checks,
        "agent": _agent_status(),
    }
