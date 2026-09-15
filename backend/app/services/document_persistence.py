"""文档层持久化（P10-1）。

职责边界
-------
::

    Agent 的 解析 / 条款切分 / 元数据抽取 结果
              │  一次请求、一个事务
              ▼
    document_block + clause + contract_metadata + contract_file.parse_status

Backend **不解析文档**：它拿到的是已经切好的段落、已经切好的条款、
已经抽好的元数据。它只做**校验、事务、持久化**。

一次写入 = 一个事务
------------------
三张表之间有硬性 FK 依赖（clause/metadata 指向 document_block.id），
而这些 id 在本事务内才产生。因此：

::

    幂等门禁 → 行锁 → DocumentBlock → flush（拿到 id）→ Clause
             → ContractMetadata → parse_status → flush → commit

任何一步失败，``session_scope`` 整体回滚 —— **绝不会留下"clause 指向不存在
的 block"这种结构断裂的半份文档**。

数据归属：文件级 vs 任务级（本模块最容易做错的一处）
------------------------------------------------
======================  ==========  ============================================
``document_block``      **文件级**   有 ``file_id`` 而**没有** ``task_id``；
                                   同一文件第二次审查时**复用、不重插、不覆盖**
``clause``              **任务级**   有 ``task_id``；每个任务各自一份
``contract_metadata``   **任务级**   同上
======================  ==========  ============================================

文件级复用是已批准的核心幂等能力（``contract_file.sha256`` UNIQUE：
"同一文件重复上传不重复解析"）。因此**不能**用同一套判据处理三者：

* 若把 block 当任务级 —— 同一文件第二次审查会**重复插入整套段落**
* 若把 clause 当文件级 —— 第二个任务的条款会**丢失**

复用时的引用怎么落回库里
----------------------
请求里的 ``start_block_index`` 等是**本次请求的下标**。当 block 是复用来的，
这些下标必须映射到**库里已有的** ``document_block.id``，否则新任务的条款
会挂到别的块上。映射靠 ``order_index``（同一文件内唯一且在请求内被校验过）：

* 库里没有该文件的 block → **全部插入**，下标 → 新 id
* 库里已有 → **逐块按 ``order_index`` 对应**，下标 → 已有 id

⚠️ 若请求的块与库里已有的块**对不上**（数量或 order_index 集合不同），说明
"同一份文件被解析出了两套不同的结构"（例如 Parser 版本变了）。此时**拒绝整批**，
绝不按位置硬套 —— 那会把条款挂到错误的块上，而且不会有任何报错。

并发：靠行锁串行化
----------------
门禁都是"先读后写"，因此必须先拿到 ``review_task`` 与 ``contract_file`` 两行的
排他锁（``SELECT ... FOR UPDATE``），否则两个并发请求会同时读到"未持久化"
然后双双写入 —— ``clause`` / ``contract_metadata`` 除主键外没有唯一约束，
数据库层拦不住。

锁顺序**固定为 先 task、后 file**：所有走这条路径的事务顺序一致，不会互相成环。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import LocatorType, ParseStatus, TaskStage
from app.core.errors import ConflictError, ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.db.models.contract import ContractFile
from app.db.models.document import Clause, ContractMetadata, DocumentBlock
from app.db.models.review_task import ReviewTask
from app.db.session import session_scope
from app.schemas.document import DocumentPersistRequest, DocumentPersistResponse
from app.services.risk_persistence import _PARAGRAPH_LOCATOR_TYPES, _normalize_ext
from app.utils.datetime_utils import utcnow

logger = get_logger(__name__)

#: 文档层落库完成后任务的阶段标记（§6.1 ``TaskStage``）。
#: 语义是"条款已切分并落库" —— 与 P9-10 风险层用的 ``REVIEWED`` 各占一段。
_DOCUMENT_STAGE = TaskStage.CLAUSED

#: 已经走过文档层（或更远）的阶段。落在其中任一者即拒绝重复写入。
_DOCUMENT_DONE_STAGES: frozenset[str] = frozenset({TaskStage.CLAUSED.value, TaskStage.REVIEWED.value})


async def persist_task_document(task_id: int, payload: DocumentPersistRequest) -> DocumentPersistResponse:
    """把一批文档层结果**原子地**写入该任务，并推进它的阶段。

    :raises NotFoundError: 任务 / 附件不存在（``TASK_NOT_FOUND`` / ``FILE_NOT_FOUND``）
    :raises ConflictError: 该任务已写入过文档结果（``DOCUMENT_ALREADY_PERSISTED``）；
        或该附件的解析状态已是终态且与本次不一致（``PARSE_STATUS_ALREADY_FINAL``）；
        或复用块的结构对不上（``DOCUMENT_BLOCKS_CONFLICT``）
    """
    async with session_scope() as session:
        # ---- 行锁：先 task、后 file（顺序固定，避免与其它路径成环）----
        task = await _load_task_for_update(session, task_id)
        contract_file = await _load_file_for_update(session, task.file_id)

        _assert_document_not_persisted(task)
        await _assert_no_document_rows(session, task_id)
        _assert_parse_status_transition(contract_file, payload.parse_status)

        # ---- 1) DocumentBlock：文件级复用 ----
        blocks, created, reused = await _persist_blocks(session, task, contract_file, payload)

        # flush 让 INSERT 真正发出去 —— **必须在这里**：新建的块此刻才拿到主键，
        # 下面的 clause/metadata 要引用它们。同时约束冲突在这里就会炸，而不是等到 commit。
        await session.flush()
        block_ids = [block.id for block in blocks]

        # ---- 2) Clause / 3) ContractMetadata：任务级 ----
        now = utcnow()
        _insert_clauses(session, task, payload, block_ids, now)
        _insert_metadata(session, task, payload, block_ids, now)

        # ---- 4) 解析状态 ----
        contract_file.parse_status = payload.parse_status
        contract_file.updated_at = now

        # ---- 5) 阶段 ----
        task.current_stage = _DOCUMENT_STAGE.value
        task.updated_at = now

        await session.flush()

        logger.info(
            "文档层持久化完成",
            extra={
                "task_id": task_id,
                "contract_id": task.contract_id,
                "file_id": task.file_id,
                "parse_status": contract_file.parse_status,
                "blocks_created": created,
                "blocks_reused": reused,
                "clauses": len(payload.clauses),
                "metadata": len(payload.metadata),
            },
        )
        return DocumentPersistResponse(
            task_id=task_id,
            parse_status=contract_file.parse_status,
            blocks_created=created,
            blocks_reused=reused,
            clauses_persisted=len(payload.clauses),
            metadata_persisted=len(payload.metadata),
            current_stage=task.current_stage,
        )


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #
def _assert_document_not_persisted(task: ReviewTask) -> None:
    """第一层：任务阶段是否已经走完文档层。

    ⚠️ 这一层不能省。``parse_status=FAILED`` 的批次**一个块都不会写** ——
    只靠行数检查，"失败过一次"就会留下"已落库却还能再写"的缺口。
    （与 P9-10 的空批次缺口是同一个形状。）
    """
    if task.current_stage in _DOCUMENT_DONE_STAGES:
        logger.warning(
            "拒绝重复写入文档层",
            extra={"task_id": task.id, "current_stage": task.current_stage},
        )
        raise ConflictError(
            f"任务 {task.id} 已处于 {task.current_stage} 阶段，文档层结果已持久化过"
            "（哪怕当时写入的是空批次），不允许重复写入或覆盖",
            code=ErrorCode.DOCUMENT_ALREADY_PERSISTED,
            details={"task_id": task.id, "current_stage": task.current_stage},
        )


async def _assert_no_document_rows(session: AsyncSession, task_id: int) -> None:
    """第二层：该任务是否真的已经有条款或元数据行（兜底）。"""
    clauses = await session.scalar(select(func.count()).select_from(Clause).where(Clause.task_id == task_id))
    metadata = await session.scalar(
        select(func.count()).select_from(ContractMetadata).where(ContractMetadata.task_id == task_id)
    )
    if clauses or metadata:
        logger.warning(
            "拒绝重复写入文档层",
            extra={"task_id": task_id, "clauses": clauses, "metadata": metadata},
        )
        raise ConflictError(
            f"任务 {task_id} 已有 {clauses} 条条款 / {metadata} 条元数据，不允许重复写入或覆盖",
            code=ErrorCode.DOCUMENT_ALREADY_PERSISTED,
            details={"task_id": task_id, "clauses": clauses, "metadata": metadata},
        )


def _assert_parse_status_transition(contract_file: ContractFile, target: str) -> None:
    """解析状态的迁移规则。**只禁止一条：``PARSED → FAILED``。**

    ::

        PENDING → PARSED   允许      首次解析成功
        PENDING → FAILED   允许      首次解析失败
        FAILED  → PARSED   允许      后续任务重新解析成功（升级）
        FAILED  → FAILED   允许      又一次失败（同值，幂等）
        PARSED  → PARSED   允许      同文件服务多个任务（同值，幂等）
        PARSED  → FAILED   禁止      409 PARSE_STATUS_ALREADY_FINAL

    为什么 ``FAILED`` 不是永久终态
    ----------------------------
    ``FAILED`` 表达的是"**某一次解析尝试**失败了"，而不是"这个文件永远读不出来"。
    它**不伴随任何文件级产物** —— 一个块都没有落库。因此后续任务重新解析成功时，
    把 ``FAILED`` 升级成 ``PARSED`` 是正确的：这才是文件第一次真正拥有
    ``document_block``。禁止它会让"解析器修好了 / 文件重新上传了"之后
    **永远无法持久化**，而库里还什么都没有 —— 那是纯粹的阻塞，没有任何保护价值。

    为什么 ``PARSED`` 才是不许覆盖的那一侧
    -----------------------------------
    ``PARSED`` 意味着**文件级**的 ``document_block`` 已经落库并被复用
    （``document_block`` 没有 ``task_id``，它属于文件）。此时若允许标成 ``FAILED``，
    就会留下"状态说解析失败、库里却躺着整套段落、而且别的任务还在复用它们"的
    自相矛盾 —— 这正是要拒绝的那种改写。

    同值重复写入（``PARSED→PARSED`` / ``FAILED→FAILED``）一律放行：同一文件
    可以服务多个审查任务（``contract_file.sha256`` 是文件级幂等键），
    第二个任务必然会再次提交同一个状态 —— 拦它就把文件复用这条已批准的能力打死了。
    """
    current = contract_file.parse_status
    if current == target:
        return
    if current != ParseStatus.PARSED.value:
        # PENDING 或 FAILED —— 都允许迁移到目标状态
        return

    logger.warning(
        "拒绝用 FAILED 覆盖已解析的文件",
        extra={"file_id": contract_file.id, "current": current, "target": target},
    )
    raise ConflictError(
        f"附件 {contract_file.id} 的解析状态已是 {current}，"
        f"不允许改写成 {target} —— 它的 document_block 已经落库并被复用",
        code=ErrorCode.PARSE_STATUS_ALREADY_FINAL,
        details={"file_id": contract_file.id, "current": current, "target": target},
    )


# --------------------------------------------------------------------------- #
# DocumentBlock：文件级复用
# --------------------------------------------------------------------------- #
async def _persist_blocks(
    session: AsyncSession,
    task: ReviewTask,
    contract_file: ContractFile,
    payload: DocumentPersistRequest,
) -> tuple[list[DocumentBlock], int, int]:
    """写入或复用该文件的块，返回**与 ``payload.blocks`` 同序的块对象**。

    ⚠️ 返回的是 ORM 对象而不是 id：新建的块此刻**还没有主键**（要等调用方 flush），
    这里若返回 ``[None] * n``，clause/metadata 就会把 ``NULL`` 写进 FK ——
    而 ``clause.start_block_id`` 恰好是 nullable，**不会报任何错**，
    库里只会留下一批"没有挂到任何块上"的条款。

    调用方在 flush 之后取 ``block.id``，那才是把"本次请求的下标"翻译成真实主键的地方。
    """
    existing = {
        block.order_index: block
        for block in (
            await session.execute(select(DocumentBlock).where(DocumentBlock.file_id == task.file_id))
        ).scalars()
    }

    if not existing:
        locator_type = _locator_type_of(contract_file.file_ext)
        blocks = [
            DocumentBlock(
                contract_id=task.contract_id,
                file_id=task.file_id,
                order_index=item.order_index,
                block_type=item.block_type.value,
                paragraph_index=item.paragraph_index,
                locator_type=locator_type,
                text=item.text,
                # ⚠️ 当前 Parser 不保留归一化前的原文（见 schemas/document.py 的说明），
                # 因此这一列在 DOCX 上**不代表真实的未归一化原文**
                raw_text=item.raw_text if item.raw_text is not None else item.text,
                char_start_in_block=item.char_start_in_block if item.char_start_in_block is not None else 0,
                char_end_in_block=(
                    item.char_end_in_block if item.char_end_in_block is not None else len(item.text)
                ),
                char_start_global=item.char_start_global,
                char_end_global=item.char_end_global,
                page_number=item.page_number,
                bbox_json=item.bbox_json,
                ocr_confidence=item.ocr_confidence,
            )
            for item in payload.blocks
        ]
        session.add_all(blocks)
        return blocks, len(blocks), 0

    # ---- 复用：请求的块必须与库里已有的块一一对应 ----
    requested = {item.order_index for item in payload.blocks}
    if requested != set(existing):
        logger.warning(
            "复用文件时块结构对不上",
            extra={
                "file_id": task.file_id,
                "requested_order_index": sorted(requested),
                "existing_order_index": sorted(existing),
            },
        )
        raise ConflictError(
            f"附件 {task.file_id} 已有的块结构与本次解析结果不一致"
            f"（本次 {len(requested)} 块，库中 {len(existing)} 块），"
            "拒绝按位置硬套 —— 那会把条款挂到错误的块上",
            code=ErrorCode.DOCUMENT_BLOCKS_CONFLICT,
            details={
                "file_id": task.file_id,
                "requested_order_index": sorted(requested),
                "existing_order_index": sorted(existing),
            },
        )
    # 复用分支：按请求顺序返回既有块，调用方统一在 flush 后取 id
    return [existing[item.order_index] for item in payload.blocks], 0, len(existing)


def _locator_type_of(file_ext: str) -> str:
    """块的定位方式，由附件扩展名派生（§10.4）。

    ⚠️ **刻意复用风险层的实现**（``risk_persistence`` 的两个私有件），而不是在这里
    再写一遍：``document_block.locator_type`` 与 ``risk_item.locator_type`` 必须永远
    一致 —— 一旦不同，前端按它选择的定位文案就会与坐标矛盾，而且不会有任何报错。
    两处口径只留一份实现，就不会漂移。
    """
    if _normalize_ext(file_ext) in _PARAGRAPH_LOCATOR_TYPES:
        return LocatorType.PARAGRAPH.value
    return LocatorType.PAGE.value


# --------------------------------------------------------------------------- #
# Clause / ContractMetadata：任务级
# --------------------------------------------------------------------------- #
def _insert_clauses(
    session: AsyncSession,
    task: ReviewTask,
    payload: DocumentPersistRequest,
    block_ids: list[int],
    now,
) -> None:
    """条款写入。段落区间在库里由 ``start_block_id`` / ``end_block_id`` 表达
    （``clause`` 表**没有**段落号列），全局偏移取首/末块的值。"""
    session.add_all(
        [
            Clause(
                contract_id=task.contract_id,
                task_id=task.id,
                clause_no=item.clause_no,
                clause_type=item.clause_type.value,
                title=item.title,
                start_block_id=block_ids[item.start_block_index],
                end_block_id=block_ids[item.end_block_index],
                char_start_global=_fallback(
                    item.char_start_global, payload.blocks[item.start_block_index].char_start_global
                ),
                char_end_global=_fallback(
                    item.char_end_global, payload.blocks[item.end_block_index].char_end_global
                ),
                page_start=item.page_start,
                page_end=item.page_end,
                text=item.text,
                extract_method=item.extract_method.value,
                confidence=item.confidence,
                created_at=now,
                updated_at=now,
            )
            for item in payload.clauses
        ]
    )


def _insert_metadata(
    session: AsyncSession,
    task: ReviewTask,
    payload: DocumentPersistRequest,
    block_ids: list[int],
    now,
) -> None:
    session.add_all(
        [
            ContractMetadata(
                contract_id=task.contract_id,
                task_id=task.id,
                field_key=item.field_key,
                field_label=item.field_label,
                field_value=item.field_value,
                value_type=item.value_type,
                confidence=item.confidence,
                source_block_id=(
                    block_ids[item.source_block_index] if item.source_block_index is not None else None
                ),
                char_start_global=item.char_start_global,
                char_end_global=item.char_end_global,
                page_number=item.page_number,
                extract_method=item.extract_method.value,
                created_at=now,
                updated_at=now,
            )
            for item in payload.metadata
        ]
    )


def _fallback(value: int | None, derived: int) -> int:
    """调用方给了就用它的；没给才用派生值。

    ``clause.char_start_global`` / ``char_end_global`` 是 NOT NULL，
    而 Agent 的 ``Clause`` 目前不产出字符偏移 —— 省略时取首/末块的全局区间，
    这是**由块的区间直接得来**的，不是服务端重新计算坐标。
    """
    return value if value is not None else derived


# --------------------------------------------------------------------------- #
# 读取（带行锁）
# --------------------------------------------------------------------------- #
async def _load_task_for_update(session: AsyncSession, task_id: int) -> ReviewTask:
    """取任务并加行锁 —— 任务级幂等的锚点。

    ``FOR UPDATE`` 是当前读而非快照读：第二个并发事务会阻塞在这里，
    等第一个提交后再读到**最新的** ``current_stage``，于是被门禁拦下。
    """
    task = (
        await session.execute(select(ReviewTask).where(ReviewTask.id == task_id).with_for_update())
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(
            f"审查任务 {task_id} 不存在",
            code=ErrorCode.TASK_NOT_FOUND,
            details={"task_id": task_id},
        )
    return task


async def _load_file_for_update(session: AsyncSession, file_id: int) -> ContractFile:
    """取附件并加行锁 —— 文件级复用（block 与 parse_status）的锚点。

    锁顺序固定为 task → file，与所有走这条路径的事务一致，不会成环。
    """
    contract_file = (
        await session.execute(select(ContractFile).where(ContractFile.id == file_id).with_for_update())
    ).scalar_one_or_none()
    if contract_file is None:
        raise NotFoundError(
            f"附件 {file_id} 不存在",
            code=ErrorCode.FILE_NOT_FOUND,
            details={"file_id": file_id},
        )
    return contract_file


__all__ = ["persist_task_document"]
