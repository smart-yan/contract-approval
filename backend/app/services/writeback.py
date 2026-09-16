"""审批回写服务（P15-2）：门禁 + 正文生成 + ``writeback_record`` 持久化。

它做什么
-------
::

    start_writeback(task_id, approval_instance_id)
        ├─ ① 回写门禁：current_stage == REVIEWED ？
        ├─ ② 取投影 → P15-1 的 renderer 生成 content_md / content_hash / idempotency_key
        └─ ③ 落库：不存在 → 新建（writing）；已失败 → 重试（writing, attempt+1）；
                  已成功 → 拒绝；**正在写** → 原样返回（不要重复发帖）

    finish_writeback(record_id, success=…)
        └─ writing → success / failed（+ finished_at）

**本阶段不调用审批系统**（真实/Mock 都没有）："发起"与"结束"之间那次网络调用由
P15-3 的 ``ApprovalClient`` 接进来。这样切分的理由见 ``schemas/writeback.py``
的说明（项目硬规则：事务里不许夹长耗时网络调用）。

回写门禁只有一条
--------------
``current_stage == REVIEWED``（P9-10 冻结语义：**AI 审查结果已持久化**），否则
409 ``WRITEBACK_NOT_READY``（``errors.py`` 早已为这个场景登记了这个码）。

⚠️ **"风险是否已人工复核"不构成门禁** —— 依据见 :func:`_ensure_writeback_ready`。

并发（两层，与项目既有手法一致）
----------------------------
* **第一层**：按 ``idempotency_key`` 取记录时加行锁（``SELECT ... FOR UPDATE``）——
  ``failed → writing`` 的重试是同一行上的"先读后写"，没有锁两个并发重试会各加一次
  ``attempt``
* **第二层**：``writeback_record.idempotency_key`` 的 **UNIQUE 约束**。两个并发
  "首次发起"都会查到"没有记录"、都会去 INSERT，输的一方捕获 ``IntegrityError``
  → 事务已回滚 → **开新事务重查** → 按既有记录返回（P4 冻结的同一套模式；
  不能在原事务里重查：MySQL 默认 REPEATABLE READ，旧快照看不见对方刚提交的行）

⚠️ ``task_id`` **不做** UNIQUE：同一任务在复核内容变化后允许有**多条**记录
（P15-1 冻结的幂等语义：同内容才去重，不是"一个任务只能回写一次"）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import WRITEBACK_STATUS_TRANSITIONS, TaskStage, WritebackStatus
from app.core.errors import ConflictError, ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.approval import ApprovalInstance
from app.db.models.review_task import ReviewTask
from app.db.models.writeback import WritebackRecord
from app.db.session import session_scope
from app.schemas.writeback import WritebackFinishResponse, WritebackStartResponse
from app.services.report_query import load_report_data
from app.services.writeback_render import WritebackContent, render_writeback
from app.utils.datetime_utils import utcnow

logger = get_logger(__name__)

__all__ = ["finish_writeback", "load_review_data_for_writeback", "start_writeback"]


# --------------------------------------------------------------------------- #
# 发起
# --------------------------------------------------------------------------- #
async def start_writeback(
    task_id: int,
    approval_instance_id: int,
    *,
    operator_id: int | None = None,
) -> WritebackStartResponse:
    """发起一次回写：过门禁、生成正文、把记录推进到 ``writing``。

    :param approval_instance_id: 目标审批单。⚠️ 本阶段**由调用方给出** ——
        "合同怎么关联到审批单"（``contract.approval_instance_id`` 目前恒为 NULL，
        待办同步尚未实现）不属于 P15-2，见报告的待裁决项。
    :param operator_id: 操作人。项目没有 ``sys_user`` / 登录体系，因此当前恒为空
        —— 与 P13 的 ``reviewer_id`` 同一条规矩：**不伪造一个用户 id**。

    :raises NotFoundError: 任务不存在（``TASK_NOT_FOUND``）
    :raises ValidationError: ``approval_instance_id`` 不存在（``VALIDATION_ERROR``）
    :raises ConflictError: 任务未审完（``WRITEBACK_NOT_READY``）；
        同一内容此前已**成功**回写过（``WRITEBACK_ALREADY_SUCCESS``）
    """
    data = await load_review_data_for_writeback(task_id)
    content = render_writeback(data.task, data.contract, data.risks)

    try:
        return await _begin_attempt(
            task_id=task_id,
            contract_id=data.contract.contract_id,
            approval_instance_id=approval_instance_id,
            content=content,
            operator_id=operator_id,
        )
    except IntegrityError as exc:
        # 并发首次发起：UNIQUE(idempotency_key) 挡下了这一方。
        # 事务已回滚，必须**开新事务**重查（旧快照看不见对方刚提交的行）。
        return await _resume_after_conflict(content, exc)


async def _begin_attempt(
    *,
    task_id: int,
    contract_id: int,
    approval_instance_id: int,
    content: WritebackContent,
    operator_id: int | None,
) -> WritebackStartResponse:
    async with session_scope() as session:
        await _ensure_instance_exists(session, approval_instance_id)

        existing = await _load_by_key_for_update(session, content.idempotency_key)
        if existing is not None:
            return _resume_existing(existing, content, operator_id)

        record = WritebackRecord(
            task_id=task_id,
            contract_id=contract_id,
            approval_instance_id=approval_instance_id,
            content_md=content.content_md,
            content_hash=content.content_hash,
            idempotency_key=content.idempotency_key,
            status=WritebackStatus.WRITING.value,
            attempt=1,
            operator_id=operator_id,
            started_at=utcnow(),
        )
        session.add(record)
        await session.flush()

        logger.info(
            "回写尝试已发起 | task_id=%s record_id=%s attempt=1 status=%s",
            task_id,
            record.id,
            record.status,
        )
        return _to_start_response(record, content, started=True)


async def _resume_after_conflict(content: WritebackContent, cause: IntegrityError) -> WritebackStartResponse:
    """并发插入失败之后的收尾：在新事务里读回那条记录，按既有记录处置。

    ⚠️ 读不到就**原样抛回** ``IntegrityError``：那说明撞的不是幂等键
    （例如某个我们没预料到的约束），不能假装成"幂等复用"把它吞掉。
    """
    async with session_scope() as session:
        existing = await _load_by_key(session, content.idempotency_key)
        if existing is None:
            raise cause

        logger.info(
            "并发发起撞上幂等键，复用了既有记录 | record_id=%s task_id=%s",
            existing.id,
            existing.task_id,
        )
        return _resume_existing(existing, content, operator_id=None)


def _resume_existing(
    existing: WritebackRecord, content: WritebackContent, operator_id: int | None
) -> WritebackStartResponse:
    """既有记录（同幂等键）的处置 —— §6.2 的迁移矩阵是唯一权威。

    ==================  ========================================================
    ``success``          409 ``WRITEBACK_ALREADY_SUCCESS``（终态，受幂等键保护）
    ``failed``           ``failed → writing``（矩阵允许的重试）：``attempt+1``、
                         清空 ``error_msg``、刷新 ``started_at``
    ``not_written``      ``not_written → writing``：同上是"第一次真正发起"
    ``writing``          **原样返回，不改任何字段** —— 矩阵里 ``writing`` 没有指向
                         自己的迁移，因此"重复点击 / 上一次还没回来"不该再发起一次
                         （``started=False``，调用方据此**不要**重复发帖）
    ==================  ========================================================
    """
    if existing.status == WritebackStatus.SUCCESS.value:
        logger.warning(
            "拒绝重复回写：同一内容已成功",
            extra={"record_id": existing.id, "task_id": existing.task_id},
        )
        raise ConflictError(
            f"任务 {existing.task_id} 的这份审查意见已经成功回写过（记录 #{existing.id}），无需重复提交",
            code=ErrorCode.WRITEBACK_ALREADY_SUCCESS,
            details={"record_id": existing.id, "task_id": existing.task_id},
        )

    if existing.status == WritebackStatus.WRITING.value:
        logger.info(
            "回写仍在进行中，复用既有记录 | record_id=%s task_id=%s",
            existing.id,
            existing.task_id,
        )
        return _to_start_response(existing, content, started=False)

    source = WritebackStatus(existing.status)
    _ensure_transition_allowed(existing, source, WritebackStatus.WRITING)

    existing.status = WritebackStatus.WRITING.value
    existing.attempt += 1
    existing.error_msg = None
    existing.started_at = utcnow()
    existing.operator_id = operator_id

    logger.info(
        "回写重试已发起 | record_id=%s task_id=%s attempt=%s",
        existing.id,
        existing.task_id,
        existing.attempt,
    )
    return _to_start_response(existing, content, started=True)


# --------------------------------------------------------------------------- #
# 结束
# --------------------------------------------------------------------------- #
async def finish_writeback(
    record_id: int,
    *,
    success: bool,
    external_comment_id: str | None = None,
    error_msg: str | None = None,
    response_body: str | None = None,
) -> WritebackFinishResponse:
    """结束一次回写尝试：``writing`` → ``success`` / ``failed``。

    由 P15-3 在拿到审批系统的结果之后调用（2xx → 成功；超时/4xx/5xx → 失败）。

    :raises NotFoundError: 记录不存在（``NOT_FOUND``）
    :raises ConflictError: 记录不在 ``writing``（``INVALID_STATE_TRANSITION``）——
        例如已经成功过（幂等锁定）或已经被结束过
    """
    target = WritebackStatus.SUCCESS if success else WritebackStatus.FAILED

    async with session_scope() as session:
        record = await _load_record_for_update(session, record_id)
        _ensure_transition_allowed(record, WritebackStatus.WRITING, target)

        record.status = target.value
        record.finished_at = utcnow()
        record.response_body = response_body
        if success:
            record.external_comment_id = external_comment_id
            record.error_msg = None
        else:
            # ⚠️ 失败原因必须**有值**：一次失败却没有原因，排查时只能看到"failed"
            record.error_msg = error_msg or "审批系统回写失败"

        await session.flush()

        logger.info(
            "回写尝试已结束 | record_id=%s task_id=%s status=%s attempt=%s",
            record.id,
            record.task_id,
            record.status,
            record.attempt,
        )
        return WritebackFinishResponse(
            record_id=record.id,
            task_id=record.task_id,
            status=record.status,
            attempt=record.attempt,
            external_comment_id=record.external_comment_id,
            error_msg=record.error_msg,
            finished_at=record.finished_at,
        )


# --------------------------------------------------------------------------- #
# ① 门禁 + 投影
# --------------------------------------------------------------------------- #
async def load_review_data_for_writeback(task_id: int):
    """取回回写正文所需的投影（task / contract / risks）。

    **先过回写门禁，再复用 P12 的只读投影**（``report_query.load_report_data``）：
    那份投影里已经含任务、合同与**带 P13 复核字段**的风险清单，正是 P15-1 renderer
    要的三份输入。自己再写一份查询等于把"任务级 / 文件级隔离"那条数据库知识复制
    一遍（见 ``workbench_query`` 对这件事的说明）。

    代价是它顺带查了 clauses / metadata / file（正文用不上）。这是**刻意**的取舍：
    多两条小 SELECT，换"投影只有一处实现"。真要优化，该由 P12 提供更窄的投影，
    而不是在这里重写一份。

    ⚠️ 它自己的门禁是 ``REPORT_NOT_READY``（同一条件的两个消费场景各用一个码，
    见 ``errors.py`` 的说明）。这里的回写门禁先跑，条件相同且 ``current_stage``
    只会向前推进，因此那条例外到不了。
    """
    async with session_scope() as session:
        stage = await session.scalar(select(ReviewTask.current_stage).where(ReviewTask.id == task_id))

    if stage is None:
        raise NotFoundError(
            f"审查任务 {task_id} 不存在",
            code=ErrorCode.TASK_NOT_FOUND,
            details={"task_id": task_id},
        )
    _ensure_writeback_ready(task_id, stage)

    return await load_report_data(task_id)


def _ensure_writeback_ready(task_id: int, current_stage: str) -> None:
    """回写门禁：任务必须已经跑完并持久化了风险（``REVIEWED``）。

    ⚠️ **门禁只有这一条 —— 风险是否已人工复核不构成门禁。** 依据是冻结设计本身：

    * §6.3：人工复核"**不是流程的一环**……结论只出现在风险详情，不参与任何综合裁决"
    * 复核**不会**推进 ``current_stage``（P13 冻结）—— 若要用它做门禁，就得先造一个
      "全部复核完成"的信号，而库里没有这个字段，也没有对应的错误码
    * §12 的回写流程里唯一的触发是"法务点击写回"，没有任何"先复核完"的前置步骤

    于是 ``PENDING`` 的风险**照常出现在正文里**（渲染层如实写"待复核"），
    由点击写回的人自行判断 —— 而不是由这一层替他决定"你还没复核完，不许写"。
    """
    if current_stage == TaskStage.REVIEWED.value:
        return

    logger.warning(
        "拒绝回写：任务尚未审完",
        extra={"task_id": task_id, "current_stage": current_stage},
    )
    raise ConflictError(
        f"任务 {task_id} 的当前阶段是 {current_stage}，尚未完成风险审查（需要 "
        f"{TaskStage.REVIEWED.value}），无法回写审批意见",
        code=ErrorCode.WRITEBACK_NOT_READY,
        details={"task_id": task_id, "current_stage": current_stage},
    )


# --------------------------------------------------------------------------- #
# 读取与校验
# --------------------------------------------------------------------------- #
async def _ensure_instance_exists(session: AsyncSession, approval_instance_id: int) -> None:
    """目标审批单必须存在。

    ⚠️ 不靠 FK 报错来发现：那会以 ``IntegrityError`` 冒出来，被并发分支当成
    "幂等键撞了"处理，最后变成一个 500。插入前先问一句，错误才有名字
    （``VALIDATION_ERROR`` = 调用方给了一个不存在的 id）。
    """
    found = await session.scalar(
        select(ApprovalInstance.id).where(ApprovalInstance.id == approval_instance_id)
    )
    if found is None:
        raise ValidationError(
            f"审批单 {approval_instance_id} 不存在，无法回写",
            code=ErrorCode.VALIDATION_ERROR,
            details={"approval_instance_id": approval_instance_id},
        )


async def _load_by_key(session: AsyncSession, key: str) -> WritebackRecord | None:
    stmt = select(WritebackRecord).where(WritebackRecord.idempotency_key == key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _load_by_key_for_update(session: AsyncSession, key: str) -> WritebackRecord | None:
    """按幂等键取记录并**锁住这一行**（存在时）。

    锁是为了 ``failed → writing`` 的重试：那是同一行上的"先读后写"，没有锁的话
    两个并发重试会各自 ``attempt+1``，重试次数就少算了一次。
    """
    stmt = select(WritebackRecord).where(WritebackRecord.idempotency_key == key).with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _load_record_for_update(session: AsyncSession, record_id: int) -> WritebackRecord:
    stmt = select(WritebackRecord).where(WritebackRecord.id == record_id).with_for_update()
    record = (await session.execute(stmt)).scalar_one_or_none()
    if record is None:
        # 用一个**已登记**的码：``errors.py`` 没有 writeback 专用的 not-found，
        # 而"新增错误码"是契约变更，不该由本步顺手做
        raise NotFoundError(
            f"回写记录 {record_id} 不存在",
            code=ErrorCode.NOT_FOUND,
            details={"record_id": record_id},
        )
    return record


def _ensure_transition_allowed(
    record: WritebackRecord, source: WritebackStatus, target: WritebackStatus
) -> None:
    """``source → target`` 在 §6.2 的迁移矩阵里吗？

    ⚠️ 先比对 ``record.status == source``：调用方说的"当前状态"与行上的实际状态
    不一致时必须拒绝（否则就成了一次"照着以为的状态去迁移"）。
    """
    if record.status != source.value:
        raise ConflictError(
            f"回写记录 {record.id} 的当前状态是 {record.status}，与预期的 {source.value} 不符",
            code=ErrorCode.INVALID_STATE_TRANSITION,
            details={"record_id": record.id, "current_status": record.status, "expected": source.value},
        )

    if target in WRITEBACK_STATUS_TRANSITIONS.get(source, frozenset()):
        return

    logger.warning(
        "拒绝非法的回写状态迁移",
        extra={"record_id": record.id, "from": source.value, "to": target.value},
    )
    raise ConflictError(
        f"回写记录 {record.id} 的当前状态是 {record.status}，不允许迁移到 {target.value}"
        "（见 §6.2 的 writeback_record 状态机）",
        code=ErrorCode.INVALID_STATE_TRANSITION,
        details={"record_id": record.id, "current_status": record.status, "target": target.value},
    )


def _to_start_response(
    record: WritebackRecord, content: WritebackContent, *, started: bool
) -> WritebackStartResponse:
    return WritebackStartResponse(
        record_id=record.id,
        task_id=record.task_id,
        contract_id=record.contract_id,
        approval_instance_id=record.approval_instance_id,
        status=record.status,
        attempt=record.attempt,
        started=started,
        content_md=content.content_md,
        content_hash=content.content_hash,
        idempotency_key=content.idempotency_key,
    )
