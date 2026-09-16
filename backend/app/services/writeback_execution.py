"""回写的**编排**：把 P15-2 的本地状态机与外部审批系统接起来（P15-3b）。

为什么单独一个模块
----------------
``services/writeback.py``（P15-2）已经冻结 —— 它管的是**本地持久化语义**
（门禁 / 幂等 / 状态机）。本模块管的是**那一次网络调用的前后**：

::

    execute_writeback(task_id, client)
      ├─ ① 审批单从哪来：ReviewTask → Contract → contract.approval_instance_id
      ├─ ② start_writeback(...)        ← P15-2 冻结（门禁 + 渲染 + 幂等 + writing）
      ├─ ③ client.find_comment(...)    ← **先问外部：这条意见是不是已经写过了**
      ├─ ④ 没写过才 client.post_comment(...)
      └─ ⑤ finish_writeback(...)       ← P15-2 冻结（writing → success / failed）

拆开的好处是"零侵入"：P15-2 一行都不用改，而"网络调用绝不进事务"这条硬规则
自然成立 —— ②⑤ 各自是独立短事务，③④ 发生在它们**之间**。

为什么"先 find 再 post"不是多余的一步
----------------------------------
「**外部已经写成功，但响应丢了**」是这条链路上最危险的场景（§12 第 5 步、§14.3
的 ``duplicate_comment`` 故障就是专门演示它）：本地记录停在 ``writing``（甚至
``failed``），可外部的评论**其实已经存在**。此时若直接重发，就要靠外部系统的
幂等键兜底；先 ``find_comment`` 则能把这种情况**认出**来 —— 少一次写请求，
也少一次"万一对方幂等没做好"的风险。

⚠️ 关键认知：本地 ``UNIQUE(idempotency_key)`` 只保证"本地不重复建记录"，
它**不知道外部执行了没有**。能回答那个问题的只有外部系统本身，
所以恢复路径的第一动作永远是 ``find_comment``。

为什么没有单独的"reconcile"接口
------------------------------
因为**同一条流程已经覆盖了它**：无论记录是新发起的（``writing``）还是上次
中断留下的（``writing`` / 重试后的 ``writing``），走的都是"先 find、必要时
按**同一个**幂等键 post"这两步 —— 后者对"外部已成功"和"外部没收到"两种情形
都安全。再加一个 ``/reconcile`` 只会让同一件事有两个入口，且两个入口的语义
迟早会漂移。触发者仍是显式的调用方（不引入 worker / 定时任务 / 自动重试）。
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.errors import AppError, ConflictError, ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.db.models.approval import ApprovalInstance
from app.db.models.contract import Contract
from app.db.models.review_task import ReviewTask
from app.db.session import session_scope
from app.integrations.approval import ApprovalClient, ApprovalSystemError
from app.schemas.writeback import WritebackFinishResponse, WritebackRunResponse
from app.services.writeback import finish_writeback, start_writeback

logger = get_logger(__name__)

__all__ = ["execute_writeback", "resolve_approval_instance_id"]


async def execute_writeback(task_id: int, *, client: ApprovalClient) -> WritebackRunResponse:
    """把一次审查意见回写到该合同关联的审批单上。

    **网络调用（``client.*``）刻意落在两个数据库事务之间** —— 项目硬规则：
    事务里不许夹长耗时调用（见 ``db/session.py``）。

    :raises NotFoundError: 任务 / 合同不存在（``TASK_NOT_FOUND`` / ``CONTRACT_NOT_FOUND``）；
        合同关联的审批单行不存在（``NOT_FOUND``）
    :raises ConflictError: 任务未审完（``WRITEBACK_NOT_READY``）；
        合同未关联审批单（``WRITEBACK_APPROVAL_NOT_READY``）；
        同一内容此前已成功回写（``WRITEBACK_ALREADY_SUCCESS``）
    """
    # ---- ① 审批单从哪来：由后端自己解析，**不接受调用方传 id** ----
    instance_id = await resolve_approval_instance_id(task_id)

    # ---- ② 本地：门禁 + 渲染 + 幂等 → writing ----
    attempt = await start_writeback(task_id, instance_id)

    # ---- ③ 先问外部：这条意见是不是已经写过了 ----
    key = attempt.idempotency_key
    try:
        existing = await client.find_comment(instance_id, key)
        if existing is not None:
            # 外部已经有这条评论 —— 可能是上一次写好之后响应丢了。
            # **不再 post**，直接把本地推到 success（这正是"响应丢失"的恢复）。
            logger.info(
                "审批系统已有该幂等键的评论，跳过写入 | task_id=%s record_id=%s external_id=%s",
                task_id,
                attempt.record_id,
                existing.external_id,
            )
            finished = await finish_writeback(
                attempt.record_id, success=True, external_comment_id=existing.external_id
            )
            return _to_run_response(finished, instance_id, posted=False)

        # ---- ④ 外部没有 → 用**同一个**幂等键写进去（绝不重新生成 key）----
        ref = await client.post_comment(instance_id, attempt.content_md, key)
    except AppError as exc:
        # 外部实现抛出的**业务性**失败（例如"审批单不存在"）：
        # ① 先把本地这次尝试如实记成 ``failed``（§6.2 对 4xx 的处置是一样的 ——
        #    不能因为调用方会看到异常，就在本地留下一条"永远在写"的记录）；
        # ② 再把错误**原样抛给调用方**：它需要看到 404/409，而不是一个看起来
        #    正常的 200。
        logger.warning(
            "审批系统拒绝写入 | task_id=%s record_id=%s code=%s",
            task_id,
            attempt.record_id,
            exc.code,
        )
        await finish_writeback(attempt.record_id, success=False, error_msg=str(exc))
        raise
    except ApprovalSystemError as exc:
        # ---- 外部调用失败 → writing → failed，原因留着（§6.2）----
        logger.warning("审批系统写入失败 | task_id=%s record_id=%s err=%s", task_id, attempt.record_id, exc)
        finished = await finish_writeback(attempt.record_id, success=False, error_msg=str(exc))
        return _to_run_response(finished, instance_id, posted=True)

    # ---- ⑤ 外部成功 → writing → success ----
    finished = await finish_writeback(attempt.record_id, success=True, external_comment_id=ref.external_id)
    return _to_run_response(finished, instance_id, posted=True)


async def resolve_approval_instance_id(task_id: int) -> int:
    """由任务解析出**目标审批单 id**：``ReviewTask → Contract → contract.approval_instance_id``。

    ⚠️ **只认这一条链**。这份合同没关联审批单时**绝不猜**：不取第一条 Mock 审批单、
    不按 ``instance_no`` 推断、也不要调用方从外面传一个 id 进来（那等于把"这份合同
    归哪个审批单"这条业务事实交给调用方编）。

    :raises NotFoundError: 任务不存在（``TASK_NOT_FOUND``）/ 合同不存在（``CONTRACT_NOT_FOUND``）；
        合同指向的审批单行不存在（``NOT_FOUND``）—— "曾经关联过、后来审批单没了"
    :raises ConflictError: ``contract.approval_instance_id`` 为 NULL
        （``WRITEBACK_APPROVAL_NOT_READY``）
    """
    async with session_scope() as session:
        task = (
            await session.execute(select(ReviewTask).where(ReviewTask.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            raise NotFoundError(
                f"审查任务 {task_id} 不存在",
                code=ErrorCode.TASK_NOT_FOUND,
                details={"task_id": task_id},
            )

        contract = (
            await session.execute(select(Contract).where(Contract.id == task.contract_id))
        ).scalar_one_or_none()
        if contract is None:
            raise NotFoundError(
                f"审查任务 {task_id} 关联的合同 {task.contract_id} 不存在",
                code=ErrorCode.CONTRACT_NOT_FOUND,
                details={"task_id": task_id, "contract_id": task.contract_id},
            )

        instance_id = contract.approval_instance_id
        if instance_id is None:
            logger.warning(
                "拒绝回写：合同未关联审批单",
                extra={"task_id": task_id, "contract_id": contract.id},
            )
            raise ConflictError(
                f"合同 {contract.id} 尚未关联审批单，无法回写审批意见",
                code=ErrorCode.WRITEBACK_APPROVAL_NOT_READY,
                details={"task_id": task_id, "contract_id": contract.id},
            )

        exists = await session.scalar(select(ApprovalInstance.id).where(ApprovalInstance.id == instance_id))
        if exists is None:
            # 关联 id 指向一行不存在的审批单（该列刻意没有 FK，见 P3 的说明）
            raise NotFoundError(
                f"合同 {contract.id} 关联的审批单 {instance_id} 不存在",
                code=ErrorCode.NOT_FOUND,
                details={"contract_id": contract.id, "approval_instance_id": instance_id},
            )

        return instance_id


def _to_run_response(
    finished: WritebackFinishResponse, approval_instance_id: int, *, posted: bool
) -> WritebackRunResponse:
    return WritebackRunResponse(
        record_id=finished.record_id,
        task_id=finished.task_id,
        approval_instance_id=approval_instance_id,
        status=finished.status,
        attempt=finished.attempt,
        external_comment_id=finished.external_comment_id,
        error_msg=finished.error_msg,
        finished_at=finished.finished_at,
        posted=posted,
    )
