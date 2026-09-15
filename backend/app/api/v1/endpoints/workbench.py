"""审查工作台查询接口（P11-4）。

本模块**只做传输层**：解析路径参数、调 Service、返回响应。
它不做任何组装、换算或过滤 —— 那些在 ``app/services/workbench_query.py`` 里。

⚠️ 与同层另外两个 ``review-tasks/{task_id}/...`` 接口的关系
---------------------------------------------------------
``documents.py`` / ``risks.py`` 是**写**入（Agent 落库），本模块是**读**出（前端展示）。
读写分成两条路径是刻意的：写入受 P10 的幂等与阶段门禁保护，读取则要能随便被人翻，
两者对失败语义、并发、性能的要求完全不同。

**这个接口不改变任何数据** —— 它是纯查询。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from app.core.logging import get_logger
from app.schemas.workbench import ReviewTaskWorkbenchResponse
from app.services.workbench_query import get_workbench

logger = get_logger(__name__)

router = APIRouter(tags=["workbench"])


@router.get(
    "/review-tasks/{task_id}/workbench",
    response_model=ReviewTaskWorkbenchResponse,
    summary="取一次审查的完整工作台数据（合同 / 原文 / 条款 / 元数据 / 风险）",
    responses={
        200: {
            "description": "查询成功。**各个集合都可能为空数组**（空文档、解析失败、"
            "还没跑风险审查），那是正常的结论，不是 404"
        },
        404: {
            "description": "任务不存在（``TASK_NOT_FOUND``），"
            "或任务关联的合同 / 附件缺失（``CONTRACT_NOT_FOUND`` / ``FILE_NOT_FOUND``）"
        },
    },
)
async def read_workbench(
    task_id: Annotated[int, Path(ge=1, description="审查任务 ID")],
) -> ReviewTaskWorkbenchResponse:
    """一次性返回工作台渲染所需的全部数据。

    **唯一的 404 是"任务不存在"** —— 集合为空一律走 200 + ``[]``。

    ``blocks`` 是该任务**附件**的段落（文件级），``clauses`` / ``metadata`` / ``risks``
    是**本任务**的（任务级）。同一个文件跑多个任务时，原文共享、结论各自独立。
    """
    return await get_workbench(task_id)


__all__ = ["read_workbench", "router"]
