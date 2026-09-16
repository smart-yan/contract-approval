"""风险的人工复核（P13-1）。

职责边界
-------
::

    一条复核结论  →  门禁 → 行锁 → 原地改写 risk_item 的复核列 → 提交

**只改人工判断，不改 AI 的判断内容**：本模块写的列只有
``review_status`` / ``review_comment`` / ``risk_level``（仅 MODIFIED）/ ``reviewed_at``
/ ``updated_at``。``risk_title`` / ``reason`` / ``legal_basis`` / ``original_text`` /
``paragraph_index`` / ``clause_id`` / ``rule_id`` 一个都不碰 —— 它们是 AI 产出的事实，
改了就没有"人工复核"可言，只剩"人工重写"。

方案 A：原地改，不建复核记录表
----------------------------
``risk_item`` 本来就有这四列（``review_status`` / ``reviewer_id`` / ``review_comment``
/ ``reviewed_at``），ER 图里也没有复核记录表。因此 P13 直接改这一行，
**不新增表、不新增迁移**。

明确接受的两个代价（经架构裁决）：

1. ``MODIFIED`` 会**覆盖** AI 原来的 ``risk_level`` —— 原始值不再留痕
2. 复核历史只有**最近一次**（``reviewed_at`` 是标量，不是时间线）

还有一个由此派生的推论，写在这里免得日后当成 bug：**从 ``MODIFIED`` 改到
``CONFIRMED`` / ``REJECTED`` 时，人工修订过的 ``risk_level`` 会保留下来**
（因为原始值已经不存在了，没有"改回去"的依据）。这是方案 A 的固有结果。

状态规则
-------
::

    PENDING ──► CONFIRMED / REJECTED / MODIFIED
                ▲    ▲    ▲
                └────┴────┘  三者之间可互相修改

**"不允许回到 PENDING"是在契约层挡住的**（``RiskReviewRequest.review_status``
接受 ``RiskReviewStatus``，且校验器拒绝 ``PENDING``），因此本模块**不需要**
再读一次当前状态做迁移判定：目标状态非 PENDING 的任何组合都是合法的，
目标状态是 PENDING 的请求根本进不到这里。少一次读、也就少一次"两处规则漂移"的机会。

并发
----
``risk_item`` 没有 version 列，因此用 ``SELECT ... FOR UPDATE`` 锁住那一行
（与 P10 锁 ``review_task`` 行是同一手法）。**没有引入乐观锁体系**。

明确接受的取舍：两个复核者同时提交时，**后提交的事务覆盖先提交的**。PATCH 的
固有语义如此；单人复核场景下不值得为它加一列 version 加一次迁移。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import TaskStage
from app.core.errors import ConflictError, ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.db.models.review_task import ReviewTask
from app.db.models.risk import RiskItem
from app.db.session import session_scope
from app.schemas.risk import RiskReviewRequest, RiskReviewResponse
from app.utils.datetime_utils import utcnow

logger = get_logger(__name__)

__all__ = ["review_risk"]


async def review_risk(task_id: int, risk_id: int, review: RiskReviewRequest) -> RiskReviewResponse:
    """对**一条**风险写下法务的复核结论。

    :raises NotFoundError: 任务不存在（``TASK_NOT_FOUND``）；任务下没有这条风险
        （``RISK_NOT_FOUND`` —— 与"风险属于别的任务"共用同一个码，见
        :func:`_load_risk_for_update`）
    :raises ConflictError: 任务尚未审完（``RISK_REVIEW_NOT_READY``）

    ⚠️ **不修改 ``review_task`` 的任何一个字段** —— 不推 ``current_stage``、
    不动 ``status`` / ``finished_at``、更不写 ``risk_level_final`` / ``conclusion``
    （综合等级是 §11.2 的评分，P12 已裁决 Backend 不生成审查结论）。
    人工复核是"AI 审完之后的一次普通状态更新"，不是流程的下一阶段。
    """
    async with session_scope() as session:
        await _ensure_task_reviewed(session, task_id)
        risk = await _load_risk_for_update(session, risk_id, task_id)

        now = utcnow()
        risk.review_status = review.review_status.value
        risk.review_comment = review.review_comment
        # 只有 MODIFIED 会带等级 —— 这一点由 RiskReviewRequest 的校验器保证，
        # 到这里 risk_level 有值 ⇔ review_status 是 MODIFIED
        if review.risk_level is not None:
            risk.risk_level = review.risk_level.value
        # ⚠️ reviewer_id **刻意不写**：项目没有 sys_user 表、没有登录体系，
        # 这一列保持 NULL。填一个编造的用户 id 比留空更糟 —— 它看起来像证据
        risk.reviewed_at = now
        risk.updated_at = now

        # flush 让 UPDATE 真正发出去 —— 约束冲突在这里就炸，而不是等到 commit
        await session.flush()

        logger.info(
            "风险人工复核完成",
            extra={
                "task_id": task_id,
                "risk_id": risk_id,
                "review_status": risk.review_status,
                "risk_level": risk.risk_level,
            },
        )

        return RiskReviewResponse(
            risk_id=risk.id,
            task_id=risk.task_id,
            risk_level=risk.risk_level,
            review_status=risk.review_status,
            review_comment=risk.review_comment,
            reviewer_id=risk.reviewer_id,
            reviewed_at=now,
        )


# --------------------------------------------------------------------------- #
# 门禁与读取
# --------------------------------------------------------------------------- #
async def _ensure_task_reviewed(session: AsyncSession, task_id: int) -> None:
    """只有审完的任务才允许复核，否则 404 / 409。

    为什么**必须**卡在 ``REVIEWED``：``CLAUSED`` 阶段文档层已落库、风险还没写，
    此时"复核"没有对象；而任务一旦到 ``REVIEWED``，风险批次就已经完整落库了
    （P10 保证整批原子写入），复核的才是完整的风险清单。

    ⚠️ 读任务行**不加锁**：这里只做一次门禁判断，不参与"先读后写"的竞态
    （``current_stage`` 只会前进到 ``REVIEWED`` 然后停住 —— P10 拒绝在
    ``REVIEWED`` 上重写风险）。给任务行加锁会把**同一个任务下所有风险的复核
    串行化**，代价大而无收益。真正需要锁的是下面那行 risk_item。
    """
    current_stage = await session.scalar(
        select(ReviewTask.current_stage).where(ReviewTask.id == task_id)
    )
    # ``current_stage`` 是 NOT NULL，取到 None 只可能是"没有这一行"
    if current_stage is None:
        raise NotFoundError(
            f"审查任务 {task_id} 不存在",
            code=ErrorCode.TASK_NOT_FOUND,
            details={"task_id": task_id},
        )

    if current_stage != TaskStage.REVIEWED.value:
        logger.warning(
            "拒绝复核未完成的任务",
            extra={"task_id": task_id, "current_stage": current_stage},
        )
        raise ConflictError(
            f"任务 {task_id} 的当前阶段是 {current_stage}，尚未完成风险审查"
            f"（需要 {TaskStage.REVIEWED.value}），无法人工复核",
            code=ErrorCode.RISK_REVIEW_NOT_READY,
            details={"task_id": task_id, "current_stage": current_stage},
        )


async def _load_risk_for_update(session: AsyncSession, risk_id: int, task_id: int) -> RiskItem:
    """取这条风险并**锁住它那一行**（``SELECT ... FOR UPDATE``）。

    **双条件** ``id = risk_id AND task_id = task_id`` 是这个接口的隔离基石：
    ``risk_item.id`` 是全局主键，只按它查的话，攻击者拿任意一个 ``task_id``
    就能改到别的任务的风险。两个条件同时成立才放行。

    两种失败（id 根本不存在 / id 存在但属于别的任务）**返回同一个 404 与同一句话**。
    分开表达（例如"存在但不属于本任务"给 403）等于告诉调用方
    "这个 id 在别处是存在的" —— 那是一条不必要的存在性泄露，而这个接口
    没有任何需要区分它们的功能。

    ``FOR UPDATE`` 是**当前读**：并发的第二个事务会阻塞在这里，等第一个提交后
    读到最新的行再改。没有它，两个复核请求会各自基于旧快照写入，
    最终结果取决于谁后 commit —— 那是"随机覆盖"而不是"后写覆盖"。
    """
    stmt = (
        select(RiskItem)
        .where(RiskItem.id == risk_id, RiskItem.task_id == task_id)
        .with_for_update()
    )
    risk = (await session.execute(stmt)).scalar_one_or_none()

    if risk is None:
        logger.warning(
            "复核目标风险不存在",
            extra={"task_id": task_id, "risk_id": risk_id},
        )
        raise NotFoundError(
            f"任务 {task_id} 下不存在风险项 {risk_id}",
            code=ErrorCode.RISK_NOT_FOUND,
            details={"task_id": task_id, "risk_id": risk_id},
        )
    return risk
