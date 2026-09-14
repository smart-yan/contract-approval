"""``/api/v1`` 业务路由汇总（架构文档 §2.1 ``api/v1/router.py``）。

main.py 只挂载这一个 router，业务端点按资源分文件加在 ``endpoints/`` 下。
新增资源时在此处 include 一行即可。

⚠️ 加新路由前请确认它属于**当前已批准的阶段** ——
P4 只批准了合同接入（``POST /api/v1/contracts``）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import contracts

api_v1_router = APIRouter()
api_v1_router.include_router(contracts.router)

__all__ = ["api_v1_router"]
