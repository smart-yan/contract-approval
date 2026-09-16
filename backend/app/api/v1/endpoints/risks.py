"""审查风险接口。

两条路径，语义相反，刻意放在同一个模块里
--------------------------------------

============  ==========================================  ==============================
``POST``      ``/review-tasks/{task_id}/risks``           **AI 产出**：整批落库（P9-10）
``PATCH``     ``/review-tasks/{task_id}/risks/{risk_id}`` **人工作出复核结论**（P13-1）
============  ==========================================  ==============================

一个写"AI 判了什么"，一个写"法务怎么看"。两者是同一张表的两个不同来源，
放在一起才能一眼看出"哪些字段归 AI、哪些归人"这条边界。

本模块**只做传输层**：解析路径参数与请求体、调 Service、返回响应。
不解析外键、不判断状态、不碰事务。

路径为什么挂在 ``review-tasks/{task_id}`` 下面
-------------------------------------------
风险项不属于"合同"这一层，它属于**某一次审查**（``risk_item.task_id`` 是
NOT NULL FK）。挂在任务下，路由本身就表达了"这批风险是这次审查的产出"，
也避免了请求体里再写一遍 ``task_id`` —— 那会制造"路径说 A、请求体说 B"
这种需要额外校验才能发现的不一致。

复核那条为什么带**两个** id
------------------------
``risk_item.id`` 是**全局**主键。只写 ``/risks/{risk_id}`` 的话，路径里没有
任何东西能说明"这条风险必须属于本次审查"，服务端只能靠查出来的行反推；
带上 ``task_id`` 之后，``id = risk_id AND task_id = task_id`` 是一条**能在
SQL 里表达**的约束，越权跨任务修改在数据层就无从发生（见
``services/risk_review.py``）。

为什么写入是 POST、复核是 PATCH
----------------------------
POST 的语义是"**创建**这次审查的风险"（写入后不可再改：已有风险则 409），
而不是"把这个资源设置成这个样子"。PUT 隐含的幂等语义会让人以为
重复调用是安全的"覆盖"，而我们明确拒绝覆盖（已落库的风险可能带有人工复核结果）。

PATCH 则是**部分更新一条已存在的资源** —— 复核改的正是那一行上的一小部分列。
它与 POST 的"拒绝覆盖"不矛盾：拒绝的是"用 AI 的新结果覆盖人的结论"，
而不是"人更新自己的结论"。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, status

from app.core.logging import get_logger
from app.schemas.risk import (
    RiskPersistRequest,
    RiskPersistResponse,
    RiskReviewRequest,
    RiskReviewResponse,
)
from app.services.risk_persistence import persist_task_risks
from app.services.risk_review import review_risk

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


@router.patch(
    "/review-tasks/{task_id}/risks/{risk_id}",
    response_model=RiskReviewResponse,
    summary="人工复核一条风险（确认 / 驳回 / 修改等级）",
    responses={
        200: {"description": "复核成功，返回该风险项复核相关列的最新值"},
        404: {
            "description": "任务不存在（``TASK_NOT_FOUND``），或该任务下没有这条风险"
            "（``RISK_NOT_FOUND`` —— **风险属于别的任务时也是这个码**，"
            "刻意不区分以免泄露存在性）"
        },
        409: {"description": "任务尚未完成风险审查（``RISK_REVIEW_NOT_READY``）"},
        422: {
            "description": "请求体校验失败。包括：``review_status`` 为 `PENDING`；"
            "`MODIFIED` 未给 ``risk_level``；``CONFIRMED`` / ``REJECTED`` 却给了 "
            "``risk_level``；以及**携带任何 AI 事实字段**"
            "（``risk_title`` / ``reason`` / ``original_text`` / ``paragraph_index`` …）"
        },
    },
)
async def review_risk_item(
    task_id: Annotated[int, Path(ge=1, description="审查任务 ID")],
    risk_id: Annotated[int, Path(ge=1, description="风险项 ID（必须属于该任务）")],
    payload: RiskReviewRequest,
) -> RiskReviewResponse:
    """写下法务对这条风险的复核结论。

    能改的只有 ``review_status`` / ``review_comment`` / ``risk_level``（仅 ``MODIFIED``）。
    ``reviewer_id`` 保持 null，``reviewed_at`` 由服务端取当前 UTC。

    **不修改任务本身的任何状态** —— 不推阶段、不动状态机、不计算综合等级。

    **允许重复提交**：同样的结论提交两次结果相同，不产生新行，也不需要
    ``Idempotency-Key``。
    """
    return await review_risk(task_id, risk_id, payload)


__all__ = ["persist_risks", "review_risk_item", "router"]
