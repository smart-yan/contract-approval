"""审查工作台查询（P11-4）。

一次请求要拿齐的东西
------------------
::

    ReviewTask ─┬─ Contract        （同一个合同）
                ├─ ContractFile    （**本任务的**附件）
                ├─ blocks[]        （**该附件**的段落 —— 文件级）
                ├─ clauses[]       （本任务 —— 任务级）
                ├─ metadata[]      （本任务）
                └─ risks[]         （本任务）

**5 条显式 SELECT**，全部在数据库侧过滤，没有一条是在 Python 循环里发出的：

1. task + contract + file（两次主键 join，各返回 1 行）
2. ``document_block WHERE file_id = ? ORDER BY order_index``
3. ``clause WHERE task_id = ? ORDER BY id``
4. ``contract_metadata WHERE task_id = ? ORDER BY id``
5. ``risk_item WHERE task_id = ? ORDER BY id``

为什么不写一条大 JOIN
------------------
``blocks × clauses × metadata × risks`` 一条 join 出来是**笛卡尔积**
（Golden Sample 就是 44 块 × 9 条款 × 9 元数据 × 2 风险 ≈ 7000 行的重复）。
5 条小查询在任何规模下都比它便宜，而且每条都各自能吃上索引。

SELECT 条数与数据量**无关**：20 块 / 15 条款 / 15 元数据 / 15 风险与各 1 条
跑出来都是同样的 5 条（实测）。

为什么在 Python 里做 id → 段落号 的换算不算 N+1
---------------------------------------------
因为那几批数据**已经整批在内存里了**：``block_by_id`` 是从第 2 条查询的结果建的字典，
查一次是 O(1) 的内存操作，不再碰数据库。N+1 说的是"循环里发查询"，
这里没有任何一次循环发出 SQL。

隔离：三条边界必须同时成立
------------------------
========================  =========================================================
``review_task.contract_id`` 它是**本任务的**合同 —— 不用 ``Contract.current_task_id``
                            （那个字段从 P4 起就没被维护过）
``document_block.file_id``  块是**文件级**的。只按 ``contract_id`` 查会把同一合同下
                            **另一份文件**的段落也捞进来，而 ``paragraph_index``
                            在每份文件里都从 0 开始 —— 混起来会把风险高亮到
                            另一份文档的同号段落，**且完全静默**
``clause/metadata/risk.task_id`` 它们是任务级的。同一个文件可以跑多个任务
                            （换规则集就新建），必须只取本任务的
========================  =========================================================
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.db.models.contract import Contract, ContractFile
from app.db.models.document import Clause, ContractMetadata, DocumentBlock
from app.db.models.review_task import ReviewTask
from app.db.models.risk import RiskItem
from app.db.session import session_scope
from app.schemas.workbench import (
    ReviewTaskWorkbenchResponse,
    WorkbenchBlock,
    WorkbenchClause,
    WorkbenchContract,
    WorkbenchFile,
    WorkbenchMetadataItem,
    WorkbenchRisk,
    WorkbenchTask,
)
from app.services.contract_query import progress_for_stage
from app.utils.file_utils import type_code_for_extension

logger = get_logger(__name__)


async def get_workbench(task_id: int) -> ReviewTaskWorkbenchResponse:
    """取一个审查任务的完整工作台数据。

    :raises NotFoundError: 任务不存在（``TASK_NOT_FOUND``）；任务的合同或附件缺失
        （数据被破坏，用对应的 NOT_FOUND 表达）

    ⚠️ **各集合为空不是错误**：空文档、解析失败、还没跑风险审查都会得到空数组，
    接口照样返回 200。只有 ``task_id`` 不存在才是 404。
    """
    async with session_scope() as session:
        task, contract, contract_file = await _load_task_tree(session, task_id)

        blocks = await _load_blocks(session, contract_file.id)
        clauses = await _load_clauses(session, task_id)
        metadata = await _load_metadata(session, task_id)
        risks = await _load_risks(session, task_id)

    # ---- 内存里的 id → 段落号 换算（不再碰数据库）----
    paragraph_of_block = {block.id: block.paragraph_index for block in blocks}

    result = ReviewTaskWorkbenchResponse(
        task=WorkbenchTask(
            task_id=task.id,
            status=task.status,
            current_stage=task.current_stage,
            # 复用 P11-3 的映射 —— 不在这里再抄一份，否则将来会出现两套口径
            progress=progress_for_stage(task.current_stage),
            created_at=task.created_at,
            finished_at=task.finished_at,
            risk_level_final=task.risk_level_final,
            conclusion=task.conclusion,
        ),
        contract=WorkbenchContract(
            contract_id=contract.id,
            contract_no=contract.contract_no,
            title=contract.title,
            contract_type=contract.contract_type,
            our_party=contract.our_party,
            counterparty=contract.counterparty,
            amount=contract.amount,
            currency=contract.currency,
            sign_date=contract.sign_date,
            effective_date=contract.effective_date,
            expire_date=contract.expire_date,
            dept=contract.dept,
        ),
        file=WorkbenchFile(
            file_id=contract_file.id,
            file_name=contract_file.file_name,
            # 表里只有扩展名，类型码由**上传时用的同一个函数**还原，口径不会漂
            file_type=type_code_for_extension(contract_file.file_ext) or contract_file.file_ext,
            sha256=contract_file.sha256,
            parse_status=contract_file.parse_status,
        ),
        blocks=[
            WorkbenchBlock(
                block_id=block.id,
                order_index=block.order_index,
                paragraph_index=block.paragraph_index,
                block_type=block.block_type,
                text=block.text,
                char_start_global=block.char_start_global,
                char_end_global=block.char_end_global,
            )
            for block in blocks
        ],
        clauses=[
            WorkbenchClause(
                clause_id=clause.id,
                clause_no=clause.clause_no,
                clause_type=clause.clause_type,
                title=clause.title,
                text=clause.text,
                start_paragraph_index=paragraph_of_block.get(clause.start_block_id),
                end_paragraph_index=paragraph_of_block.get(clause.end_block_id),
                start_block_id=clause.start_block_id,
                end_block_id=clause.end_block_id,
            )
            for clause in clauses
        ],
        metadata=[
            WorkbenchMetadataItem(
                field_key=item.field_key,
                field_label=item.field_label,
                field_value=item.field_value,
                value_type=item.value_type,
                extract_method=item.extract_method,
                source_block_id=item.source_block_id,
                source_paragraph_index=paragraph_of_block.get(item.source_block_id),
            )
            for item in metadata
        ],
        risks=[
            WorkbenchRisk(
                risk_id=risk.id,
                risk_code=risk.risk_code,
                risk_title=risk.risk_title,
                dimension=risk.dimension,
                risk_level=risk.risk_level,
                source=risk.source,
                reason=risk.reason,
                legal_basis=risk.legal_basis,
                original_text=risk.original_text,
                paragraph_index=risk.paragraph_index,
                clause_id=risk.clause_id,
                locator_type=risk.locator_type,
                review_status=risk.review_status,
            )
            for risk in risks
        ],
    )

    _warn_on_foreign_clause_ids(risks, {clause.id for clause in clauses}, task_id)

    logger.info(
        "工作台查询完成 | task_id=%s blocks=%d clauses=%d metadata=%d risks=%d",
        task_id,
        len(blocks),
        len(clauses),
        len(metadata),
        len(risks),
    )
    return result


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
async def _load_task_tree(session: AsyncSession, task_id: int) -> tuple[ReviewTask, Contract, ContractFile]:
    """**一条 SELECT** 取回 task + contract + file（两次主键 join，最多 1 行）。

    **合同与附件都从任务上取** —— ``ReviewTask.contract_id`` / ``ReviewTask.file_id``
    是这两条关系的**唯一权威**。刻意不用 ``Contract.current_task_id``：
    那个字段从 P4 起就没有被维护过（恒为 NULL），拿它反查会得到"这个合同没有任务"。

    用 ``LEFT JOIN`` 而不是 ``INNER JOIN`` 是为了**保住失败诊断**：
    内连接下"任务不存在"与"任务在、但它的合同没了"都会返回 0 行，
    调用方就分不清该报哪个错误码。左连接下三种情况各自可辨：

    ====================  ==============================================
    0 行                 任务不存在 → ``TASK_NOT_FOUND``
    contract 为 None     任务在、合同没了（数据被破坏）→ ``CONTRACT_NOT_FOUND``
    file 为 None         任务在、附件没了 → ``FILE_NOT_FOUND``
    ====================  ==============================================

    后两种**不返回半份数据**：合同上的两个 FK 都是 NOT NULL，查不到只可能是数据被
    外力破坏，此时给出半份工作台比报错更糟。
    """
    stmt = (
        select(ReviewTask, Contract, ContractFile)
        .outerjoin(Contract, Contract.id == ReviewTask.contract_id)
        .outerjoin(ContractFile, ContractFile.id == ReviewTask.file_id)
        .where(ReviewTask.id == task_id)
    )
    row = (await session.execute(stmt)).first()

    if row is None:
        raise NotFoundError(
            f"审查任务 {task_id} 不存在",
            code=ErrorCode.TASK_NOT_FOUND,
            details={"task_id": task_id},
        )

    task, contract, contract_file = row
    if contract is None:
        raise NotFoundError(
            f"任务 {task_id} 关联的合同 {task.contract_id} 不存在",
            code=ErrorCode.CONTRACT_NOT_FOUND,
            details={"task_id": task_id, "contract_id": task.contract_id},
        )
    if contract_file is None:
        raise NotFoundError(
            f"任务 {task_id} 关联的附件 {task.file_id} 不存在",
            code=ErrorCode.FILE_NOT_FOUND,
            details={"task_id": task_id, "file_id": task.file_id},
        )

    return task, contract, contract_file


async def _load_blocks(session: AsyncSession, file_id: int) -> list[DocumentBlock]:
    """该**附件**的段落，按 ``order_index`` 升序。

    ⚠️ 按 ``file_id`` 而不是 ``contract_id`` —— 块是文件级的，而
    ``paragraph_index`` 在每份文件里各自从 0 开始。
    """
    stmt = (
        select(DocumentBlock)
        .where(DocumentBlock.file_id == file_id)
        .order_by(DocumentBlock.order_index.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def _load_clauses(session: AsyncSession, task_id: int) -> list[Clause]:
    """本任务的条款，按 ``id`` 升序（= 落库顺序 = 切分顺序）。

    ⚠️ ``clause`` 表没有显式的排序列（Agent 的 ``clause_index`` 没有落库），
    因此顺序**只能**靠 ``id`` —— 它与 P10 的插入顺序一致。这一点在
    ``WorkbenchClause`` 的排序断言里被固定住。
    """
    stmt = select(Clause).where(Clause.task_id == task_id).order_by(Clause.id.asc())
    return list((await session.execute(stmt)).scalars())


async def _load_metadata(session: AsyncSession, task_id: int) -> list[ContractMetadata]:
    stmt = (
        select(ContractMetadata)
        .where(ContractMetadata.task_id == task_id)
        .order_by(ContractMetadata.id.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def _load_risks(session: AsyncSession, task_id: int) -> list[RiskItem]:
    stmt = select(RiskItem).where(RiskItem.task_id == task_id).order_by(RiskItem.id.asc())
    return list((await session.execute(stmt)).scalars())


def _warn_on_foreign_clause_ids(risks: list[RiskItem], own_clause_ids: set[int], task_id: int) -> None:
    """风险挂到了**不属于本任务**的条款上时记一条 warning。

    写入侧是安全的（P10 的 ``_resolve_clause_id`` 只在本任务的条款里找），因此这通常
    意味着数据被外力改过。**只记日志、不改写返回值**：``clause_id`` 是数据库里的事实，
    把它抹掉或替换掉都是伪造。前端拿它去 ``clauses`` 数组里找，找不到就不显示条款——
    降级是安全的。
    """
    foreign = sorted({risk.clause_id for risk in risks if risk.clause_id is not None} - own_clause_ids)
    if foreign:
        logger.warning(
            "工作台发现跨任务的风险-条款关联 | task_id=%s 不属于本任务的 clause_id=%s",
            task_id,
            foreign,
        )


__all__ = ["get_workbench"]
