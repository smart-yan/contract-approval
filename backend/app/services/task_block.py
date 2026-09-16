"""把审查任务标记为阻塞（P14-4）。

职责边界
-------
::

    阻塞原因  →  状态机校验 → 行锁 → 写 status / block_reason_*  → 提交

它**只写三个字段**：``status`` / ``block_reason_code`` / ``block_reason_msg``。

刻意**不碰**的字段
----------------
========================  ==================================================
``current_stage``         阶段表达"已完成到哪一步"（断点续跑依据）。任务失败
                          **不代表阶段回退或前进** —— 一份已切分完条款的输入
                          跑挂了，它的阶段仍然诚实地是 ``CLAUSED``
``finished_at``           "任务结束时刻"。本阶段只把任务标成**阻塞**（等人处理），
                          不是"结束" —— 填了它就会与 ``status`` 自相矛盾
``risk_level_final`` /    业务结论。评分器没实现，阻塞更不是产出结论的时机
``conclusion`` / ``summary``
``error_msg``             那一列是**排查用的自由文本**（Backend 自己的失败）；
                          Agent 的失败原因走 ``block_reason_*`` 这一对结构化字段。
                          两处都写会让"这条原因是谁写的"说不清
========================  ==================================================

为什么必须过状态机
----------------
§6.1 的 ``TASK_STATUS_TRANSITIONS`` 是**唯一权威**。本接口不自己判断"能不能阻塞"，
而是把 ``BLOCKED`` 当成一次普通的迁移去问那张矩阵：

* ``pending`` / ``parsing`` / ``reviewing`` → 允许阻塞（矩阵里都有 ``BLOCKED``）
* ``blocked`` → 不允许（矩阵里 ``blocked`` 只指向 ``pending``）—— **重复阻塞会被拒**
* ``completed`` → 不允许（终态）

这样"哪些状态能被阻塞"永远只有一处定义，将来状态机改了这里自动跟着改。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import TASK_STATUS_TRANSITIONS, BlockReasonCode, TaskStatus
from app.core.errors import ConflictError, ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.review_task import ReviewTask
from app.db.session import session_scope
from app.schemas.task import TaskBlockRequest, TaskBlockResponse
from app.utils.datetime_utils import utcnow

logger = get_logger(__name__)

__all__ = ["block_task"]


async def block_task(task_id: int, payload: TaskBlockRequest) -> TaskBlockResponse:
    """把任务置为 ``blocked`` 并记下原因。

    :raises NotFoundError: 任务不存在（``TASK_NOT_FOUND``）
    :raises ConflictError: 当前状态不允许迁移到 ``blocked``
        （``INVALID_STATE_TRANSITION``）—— 例如已经处于 ``blocked`` 或 ``completed``

    ⚠️ 本接口**幂等性有限**：重复阻塞会返回 409 而不是静默成功。理由是第二次调用
    通常意味着调用方对状态的判断已经过期（例如两个后台任务都以为自己失败了），
    静默成功会把"有人重复处理"这件事藏起来。
    """
    async with session_scope() as session:
        task = await _load_task_for_update(session, task_id)
        _ensure_transition_allowed(task.id, task.status)
        _ensure_reason_code_known(task.id, payload.block_reason_code)

        now = utcnow()
        previous_status = task.status
        task.status = TaskStatus.BLOCKED.value
        task.block_reason_code = payload.block_reason_code
        task.block_reason_msg = payload.block_reason_msg
        task.updated_at = now

        # flush 让 UPDATE 真正发出去 —— 约束冲突在这里就炸，而不是等到 commit
        await session.flush()

        logger.info(
            "任务已标记为阻塞 | task_id=%s from_status=%s reason_code=%s stage=%s",
            task_id,
            previous_status,
            payload.block_reason_code,
            task.current_stage,
        )

        return TaskBlockResponse(
            task_id=task.id,
            status=task.status,
            current_stage=task.current_stage,
            block_reason_code=task.block_reason_code or "",
            block_reason_msg=task.block_reason_msg or "",
        )


# --------------------------------------------------------------------------- #
# 读取与校验
# --------------------------------------------------------------------------- #
async def _load_task_for_update(session: AsyncSession, task_id: int) -> ReviewTask:
    """取任务并**锁住它那一行**（``SELECT ... FOR UPDATE``）。

    与 P9-10 的风险写入、P13-1 的复核同一手法：本事务接下来要做"先读状态、
    再决定改不改"的判断，没有行锁的话两个并发请求会同时读到同一个旧状态，
    双双通过状态机校验然后各自写入 —— 后一个会**覆盖**前一个的阻塞原因。
    """
    stmt = select(ReviewTask).where(ReviewTask.id == task_id).with_for_update()
    task = (await session.execute(stmt)).scalar_one_or_none()

    if task is None:
        raise NotFoundError(
            f"审查任务 {task_id} 不存在",
            code=ErrorCode.TASK_NOT_FOUND,
            details={"task_id": task_id},
        )
    return task


def _ensure_transition_allowed(task_id: int, current_status: str) -> None:
    """``current_status → blocked`` 是否在 §6.1 的迁移矩阵里。

    **不认得的当前状态一律拒绝**（``allowed`` 落空集）：矩阵是白名单，
    遇到没登记的状态时拒绝比放行安全 —— 放行等于对一个谁都没想清楚的状态动手。
    """
    try:
        current = TaskStatus(current_status)
    except ValueError:
        allowed: frozenset[TaskStatus] = frozenset()
    else:
        allowed = TASK_STATUS_TRANSITIONS.get(current, frozenset())

    if TaskStatus.BLOCKED not in allowed:
        logger.warning(
            "拒绝非法的阻塞迁移",
            extra={"task_id": task_id, "current_status": current_status},
        )
        raise ConflictError(
            f"任务 {task_id} 的当前状态是 {current_status}，不允许迁移到 "
            f"{TaskStatus.BLOCKED.value}（见 §6.1 的状态迁移矩阵）",
            code=ErrorCode.INVALID_STATE_TRANSITION,
            details={"task_id": task_id, "current_status": current_status},
        )


def _ensure_reason_code_known(task_id: int, reason_code: str) -> None:
    """``block_reason_code`` 必须是登记过的枚举值。

    ⚠️ 这一列**没有** CHECK 约束（§7.2 没规定），因此校验只能在应用层做。
    不校验的话，"可统计、可自愈、前端可按键给出修复引导"这三条全会失效 ——
    而它们正是 §6.1 把这个字段枚举化的**全部理由**。
    """
    if reason_code in {code.value for code in BlockReasonCode}:
        return

    logger.warning(
        "拒绝未登记的阻塞原因枚举",
        extra={"task_id": task_id, "block_reason_code": reason_code},
    )
    raise ValidationError(
        f"block_reason_code {reason_code!r} 不在 constants.BlockReasonCode 里",
        code=ErrorCode.VALIDATION_ERROR,
        details={"task_id": task_id, "block_reason_code": reason_code},
    )
