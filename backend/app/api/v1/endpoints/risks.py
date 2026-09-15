"""审查风险写入接口（P9-10）。

本模块**只做传输层**：解析路径参数与请求体、调 Service、返回响应。
不解析外键、不判断状态、不碰事务 —— 那些在
``app/services/risk_persistence.py`` 里，且整体处在一个事务内。

路径为什么挂在 ``review-tasks/{task_id}`` 下面
-------------------------------------------
风险项不属于"合同"这一层，它属于**某一次审查**（``risk_item.task_id`` 是
NOT NULL FK）。挂在任务下，路由本身就表达了"这批风险是这次审查的产出"，
也避免了请求体里再写一遍 ``task_id`` —— 那会制造"路径说 A、请求体说 B"
这种需要额外校验才能发现的不一致。

为什么是 POST 而不是 PUT
----------------------
语义是"**创建**这次审查的风险"（写入后不可再改：已有风险则 409），
而不是"把这个资源设置成这个样子"。PUT 隐含的幂等语义会让人以为
重复调用是安全的"覆盖"，而我们明确拒绝覆盖（已落库的风险可能带有人工复核结果）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, status

from app.core.logging import get_logger
from app.schemas.risk import RiskPersistRequest, RiskPersistResponse
from app.services.risk_persistence import persist_task_risks

logger = get_logger(__name__)

router = APIRouter(tags=["risks"])


@router.post(
    "/review-tasks/{task_id}/risks",
    response_model=RiskPersistResponse,
    status_code=status.HTTP_201_CREATED,
    summary="整批持久化一次审查的风险并结束该任务",
    responses={
        201: {"description": "整批写入成功，任务已置为已审查"},
        404: {
            "description": "任务/合同/附件不存在（``TASK_NOT_FOUND`` / ``CONTRACT_NOT_FOUND`` / "
            "``FILE_NOT_FOUND``），或某个 ``risk_code`` 在该任务的规则集里找不到（``RULE_NOT_FOUND``）"
        },
        409: {"description": "该任务已经写入过风险（``TASK_ALREADY_PERSISTED``）"},
        422: {"description": "请求体校验失败，或条款归属出现歧义（``VALIDATION_ERROR``）"},
    },
)
async def persist_risks(
    task_id: Annotated[int, Path(ge=1, description="审查任务 ID")],
    payload: RiskPersistRequest,
) -> RiskPersistResponse:
    """把一批风险**原子地**写入该任务，并把任务置为已审查。

    请求体里**不能**指定 ``review_status`` / ``locator_type`` / ``rule_id`` /
    ``clause_id`` —— 它们要么由服务端固定，要么由服务端从附件类型与业务编码解析
    （见 ``app/schemas/risk.py`` 的说明）。

    任何一条风险校验不过 → **整批不写**；风险写入与任务状态更新在同一个事务里，
    要么全成、要么全不成。
    """
    return await persist_task_risks(task_id, payload.risks)


__all__ = ["persist_risks", "router"]
