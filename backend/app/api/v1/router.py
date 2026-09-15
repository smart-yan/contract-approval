"""``/api/v1`` 业务路由汇总（架构文档 §2.1 ``api/v1/router.py``）。

main.py 只挂载这一个 router，业务端点按资源分文件加在 ``endpoints/`` 下。
新增资源时在此处 include 一行即可。

⚠️ 加新路由前请确认它属于**当前已批准的阶段** ——
P4 批准了合同接入（``POST /api/v1/contracts``），
P8-0 批准了规则读取（``GET /api/v1/rule-sets``），
P9-10 批准了风险写入（``POST /api/v1/review-tasks/{task_id}/risks``），
P10-1 批准了文档层写入（``POST /api/v1/review-tasks/{task_id}/document``），
P11-3/4 批准了合同列表与工作台查询（``GET /api/v1/contracts``、
``GET /api/v1/review-tasks/{task_id}/workbench``）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import contracts, documents, risks, rule_sets, workbench

api_v1_router = APIRouter()
api_v1_router.include_router(contracts.router)
api_v1_router.include_router(rule_sets.router)
api_v1_router.include_router(risks.router)
api_v1_router.include_router(documents.router)
api_v1_router.include_router(workbench.router)

__all__ = ["api_v1_router"]
