"""审查任务的状态写入口（P14-4）。

本模块**只做传输层**：解析路径参数与请求体、调 Service、返回响应。
状态机校验、行锁、事务都在 ``app/services/task_block.py`` 里。

为什么这个接口存在
----------------
P14-4 起 Agent 把整张图放到**后台**执行，HTTP 请求提前返回 202。于是出现一个
以前不存在的情况：**图跑挂了，而请求早已结束** —— 没有人能再"顺手"返回一个错误。

Agent 需要一个地方把"这次后台执行失败了"如实记在任务上。它只能走 HTTP
（P5 的边界：Agent 不碰数据库），所以这个接口就是那条通道。

为什么只有 ``block`` 一个动作
--------------------------
见 ``app/schemas/task.py`` 的说明：一个通用的状态写接口会把状态机的权威
从 Backend 挪到调用方，而 §6.1 的迁移矩阵是声明过的唯一权威。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from app.core.logging import get_logger
from app.schemas.task import TaskBlockRequest, TaskBlockResponse
from app.services.task_block import block_task

logger = get_logger(__name__)

router = APIRouter(tags=["tasks"])


@router.post(
    "/review-tasks/{task_id}/block",
    response_model=TaskBlockResponse,
    summary="把审查任务标记为阻塞（后台执行失败时由 Agent 调用）",
    responses={
        200: {"description": "已写入 blocked 与阻塞原因"},
        404: {"description": "任务不存在（``TASK_NOT_FOUND``）"},
        409: {
            "description": "当前状态不允许迁移到 ``blocked``（``INVALID_STATE_TRANSITION``）——"
            "例如已阻塞（重复阻塞）或已完成（终态）"
        },
        422: {"description": "``block_reason_code`` 不在 ``constants.BlockReasonCode`` 里"},
    },
)
async def block_review_task(
    task_id: Annotated[int, Path(ge=1, description="审查任务 ID")],
    payload: TaskBlockRequest,
) -> TaskBlockResponse:
    """把任务置为 ``blocked`` 并记下原因。

    ⚠️ **只改 ``status`` / ``block_reason_code`` / ``block_reason_msg``** ——
    ``current_stage`` 表达"已完成到哪一步"，任务失败不代表阶段回退或前进，
    因此本接口刻意不动它（见 service 的字段边界表）。
    """
    return await block_task(task_id, payload)


__all__ = ["block_review_task", "router"]
