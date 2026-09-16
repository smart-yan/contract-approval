"""审批意见回写接口（P15-3b）。

本模块**只做传输层**：解析路径参数、调 Service、返回响应。门禁、幂等、状态机、
以及"审批单从哪来"都在 ``app/services/writeback_execution.py`` 与
``app/services/writeback.py`` 里。

路径口径
-------
文档 §8 写的是 ``POST /tasks/{id}/writeback``，而 P11 之后 Backend 的实际约定是
``/api/v1/review-tasks/{task_id}/...``（``reports`` / ``workbench`` / ``risks`` /
``documents`` / ``block`` 全部如此）。**沿用实际约定**，不机械照抄旧文档路径。

为什么只有一个端点（没有 ``/reconcile``）
--------------------------------------
"正常回写"与"WRITING 恢复"在本设计里是**同一条流程**：先按幂等键问外部有没有，
没有才按**同一个**幂等键写。它对"外部没收到"和"外部其实已经写好、只是响应丢了"
两种情形都安全（详见 service 的 docstring）。多加一个 ``/reconcile`` 会让同一件事
有两个入口，两者迟早漂移。

调用方**不能**提交 ``approval_instance_id``：目标审批单由后端沿
``ReviewTask → Contract → contract.approval_instance_id`` 解析 —— 那是业务事实，
不该由请求体提供。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from app.core.logging import get_logger
from app.integrations.approval import get_approval_client
from app.schemas.writeback import WritebackRunResponse
from app.services.writeback_execution import execute_writeback

logger = get_logger(__name__)

router = APIRouter(tags=["writeback"])


@router.post(
    "/review-tasks/{task_id}/writeback",
    response_model=WritebackRunResponse,
    summary="把审查意见回写到该合同关联的审批单（幂等）",
    responses={
        200: {
            "description": "调用完成。``status=success`` 表示意见已在审批系统里；"
            "``status=failed`` 表示外部写入失败（原因在 ``error_msg``），可用**同一个**幂等键重试"
        },
        404: {
            "description": "任务 / 合同不存在（``TASK_NOT_FOUND`` / ``CONTRACT_NOT_FOUND``）；"
            "或合同关联的审批单行不存在（``NOT_FOUND``）"
        },
        409: {
            "description": "任务未审完（``WRITEBACK_NOT_READY``）；"
            "合同尚未关联审批单（``WRITEBACK_APPROVAL_NOT_READY``）；"
            "同一内容已成功回写过（``WRITEBACK_ALREADY_SUCCESS``）"
        },
    },
)
async def create_writeback(
    task_id: Annotated[int, Path(ge=1, description="审查任务 ID")],
) -> WritebackRunResponse:
    """把该任务最新的审查意见回写到审批单评论区。

    **幂等**：同内容（同一 ``writeback_record``）重复提交不会产生第二条评论 ——
    外部系统按幂等键去重，本地也会先查一次。
    """
    return await execute_writeback(task_id, client=get_approval_client())


__all__ = ["create_writeback", "router"]
