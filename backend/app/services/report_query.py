"""审查报告的数据查询（P12-2）。

职责边界
-------
::

    DB  →  4 条显式 SELECT  →  ReportData（纯数据）

本模块是**唯一**知道"报告读哪些表"的地方。它做什么：

* 判 404（任务 / 合同 / 附件不存在）
* 判 409（任务还没审完）
* 按正确的键做**隔离**，把 ORM 行装配成 :class:`~app.services.report_render.ReportData`

它**不做**什么：

* 不生成任何文字 —— 渲染在 :mod:`app.services.report_render`
* 不写库、不改 ``review_task``（纯读，报告是只读投影）
* 不重新计算风险等级、不合并风险、不产生审查结论（那些是 Agent 的活，P12 不重跑）

4 条 SELECT，与数据量无关
----------------------
============================  ==========================================
1  ``review_task ⟕ contract ⟕ contract_file``  两条主键 join，最多 1 行
2  ``clause WHERE task_id = ?``                 ORDER BY id
3  ``contract_metadata WHERE task_id = ?``      ORDER BY id
4  ``risk_item WHERE task_id = ?``              ORDER BY id
============================  ==========================================

与 P11 工作台（5 条）的差别只有一处：**不查 ``document_block``**。理由是报告的
MVP 内容用不到它 —— 风险展示的是 ``original_text``（P10 冻结语义：命中的原文
片段）加 ``paragraph_index``，条款展示的是 ``clause.text``，两者都在已有的表里。
为了"万一以后要显示整段原文"先查一遍 44 个 block，是把成本花在一个没做的需求上。
真要加时是**一条 SELECT** 的事，且必须按 ``file_id`` 查（见下方隔离表）。

刻意**不写**一条大 JOIN：blocks × clauses × metadata × risks 是笛卡尔积。
也不使用 ``relationship`` / ``joinedload`` / ``selectinload`` —— ``Base`` 下的模型
一个 ``relationship()`` 都没有，这是项目约定（见 ``contract_query.py`` 的说明）。

隔离：三条边界必须同时成立
------------------------
==============================  ============================================
``review_task.contract_id``     本任务的合同。**不用** ``contract.current_task_id``
                                （那个字段从 P4 起就没被维护过，恒为 NULL）
``clause`` / ``contract_metadata`` / ``risk_item`` 一律按 ``task_id``
                                它们是**任务级**的。同一个附件可以跑多个任务
                                （换规则集就新建），按 ``contract_id`` 查会把
                                另一次审查的条款和风险混进本报告
``document_block`` **当前不查**  将来若查，必须按 ``file_id``：块是**文件级**的，
                                而 ``paragraph_index`` 在每份文件里都从 0 开始，
                                按 ``contract_id`` 查会把另一份文件的同号段落
                                捞进来，**且完全静默**
==============================  ============================================

**报告只属于当前 task** —— 这也是本模块与 P11 工作台共用的一条硬规则。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import TaskStage
from app.core.errors import ConflictError, ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.db.models.contract import Contract, ContractFile
from app.db.models.document import Clause, ContractMetadata
from app.db.models.review_task import ReviewTask
from app.db.models.risk import RiskItem
from app.db.session import session_scope
from app.services.report_render import (
    ReportClause,
    ReportContract,
    ReportData,
    ReportFile,
    ReportMetadataItem,
    ReportRisk,
    ReportTask,
)
from app.utils.file_utils import type_code_for_extension

logger = get_logger(__name__)

__all__ = ["load_report_data"]


async def load_report_data(task_id: int) -> ReportData:
    """取生成一次审查报告所需的全部数据。

    :raises NotFoundError: 任务不存在（``TASK_NOT_FOUND``）；任务的合同或附件缺失
        （数据被破坏，用对应的 NOT_FOUND 表达 —— 不返回半份数据）
    :raises ConflictError: 任务尚未审查完成（``REPORT_NOT_READY``）。
        见 :func:`_ensure_reportable` 为什么这不是 404

    ⚠️ **各集合为空不是错误**：解析失败、空文档、这次审查没发现风险，都会得到
    空集合，报告照样能生成（它会如实写"未发现风险"）。
    """
    async with session_scope() as session:
        task, contract, contract_file = await _load_task_tree(session, task_id)

        # ⚠️ 门禁必须在**同一次请求内、读完视图数据之前**判定，且用的是刚读到的
        # ``current_stage`` —— 不是调用方传进来的、可能已经过期的值。
        _ensure_reportable(task.id, task.current_stage)

        clauses = await _load_clauses(session, task_id)
        metadata = await _load_metadata(session, task_id)
        risks = await _load_risks(session, task_id)

    data = _assemble(task, contract, contract_file, clauses, metadata, risks)

    logger.info(
        "报告数据查询完成 | task_id=%s clauses=%d metadata=%d risks=%d",
        task_id,
        len(data.clauses),
        len(data.metadata),
        len(data.risks),
    )
    return data


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #
def _ensure_reportable(task_id: int, current_stage: str) -> None:
    """任务审完了才允许出报告，否则 409 ``REPORT_NOT_READY``。

    为什么是 **409 而不是 404**：任务**存在**，只是还不能出报告。用 404 会让前端
    把"还在审查中"显示成"任务不存在"，把用户引到完全错误的方向。

    为什么**必须**卡在 ``REVIEWED``：``CLAUSED`` 阶段文档层已落库，但**风险还没写**
    （风险持久化会把阶段推到 ``REVIEWED``）。此时生成的报告会是一份**零风险**的
    报告 —— 而"报告上写着没有风险"与"还没审完"是两件完全不同的事，
    读者无从分辨。宁可 409，也不要给一份看起来正常、实则有害的文档。

    ⚠️ 复用既有的 ``REPORT_NOT_READY``（``errors.py`` 已登记为 409 USER_ERROR），
    **不新增错误码**。
    """
    if current_stage == TaskStage.REVIEWED.value:
        return

    logger.warning(
        "拒绝生成未完成任务的报告",
        extra={"task_id": task_id, "current_stage": current_stage},
    )
    raise ConflictError(
        f"任务 {task_id} 的当前阶段是 {current_stage}，尚未完成风险审查"
        f"（需要 {TaskStage.REVIEWED.value}），无法生成审查报告",
        code=ErrorCode.REPORT_NOT_READY,
        details={"task_id": task_id, "current_stage": current_stage},
    )


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
async def _load_task_tree(session: AsyncSession, task_id: int) -> tuple[ReviewTask, Contract, ContractFile]:
    """**一条 SELECT** 取回 task + contract + file（两次主键 join，最多 1 行）。

    与 ``workbench_query._load_task_tree`` 同构（含同样用 ``LEFT JOIN`` 保住的三态
    诊断）。刻意**各写一份**而不是跨模块 import 那个私有函数：两份读模型是彼此独立
    的投影，让报告耦合到工作台的私有实现，等于让 P11 的一次内部重构无声地改掉
    报告的 404 行为。等第三处也需要时再抽公共 helper —— 现在只有两处。

    用 ``LEFT JOIN`` 而不是 ``INNER JOIN`` 是为了保住失败诊断：内连接下"任务不存在"
    与"任务在、但它的合同没了"都会返回 0 行，调用方分不清该报哪个错。

    ====================  ==============================================
    0 行                  任务不存在 → ``TASK_NOT_FOUND``
    contract 为 None      任务在、合同没了（数据被破坏）→ ``CONTRACT_NOT_FOUND``
    file 为 None          任务在、附件没了 → ``FILE_NOT_FOUND``
    ====================  ==============================================

    后两种**不返回半份数据**：合同上的两个 FK 都是 NOT NULL，查不到只可能是数据被
    外力破坏，此时给出一份缺了合同信息的报告比报错更糟。
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


async def _load_clauses(session: AsyncSession, task_id: int) -> list[Clause]:
    """本任务的条款，按 ``id`` 升序（= 落库顺序 = 切分顺序）。

    ⚠️ ``clause`` 表没有显式排序列（Agent 的 ``clause_index`` 没有落库），
    顺序只能靠 ``id``，它与 P10 的插入顺序一致。
    """
    stmt = select(Clause).where(Clause.task_id == task_id).order_by(Clause.id.asc())
    return list((await session.execute(stmt)).scalars())


async def _load_metadata(session: AsyncSession, task_id: int) -> list[ContractMetadata]:
    """本任务提取到的合同信息，按 ``id`` 升序（= 提取顺序）。"""
    stmt = (
        select(ContractMetadata)
        .where(ContractMetadata.task_id == task_id)
        .order_by(ContractMetadata.id.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def _load_risks(session: AsyncSession, task_id: int) -> list[RiskItem]:
    """本任务的风险，按 ``id`` 升序（= 落库顺序）。

    ⚠️ 报告**不重排**成"按等级排序"：``risk_level`` 的排序口径属于展示决策，
    放到渲染层由模板决定。查询层只保证顺序**稳定**。
    """
    stmt = select(RiskItem).where(RiskItem.task_id == task_id).order_by(RiskItem.id.asc())
    return list((await session.execute(stmt)).scalars())


# --------------------------------------------------------------------------- #
# 装配（纯函数：ORM 行 → ReportData）
# --------------------------------------------------------------------------- #
def _assemble(
    task: ReviewTask,
    contract: Contract,
    contract_file: ContractFile,
    clauses: list[Clause],
    metadata: list[ContractMetadata],
    risks: list[RiskItem],
) -> ReportData:
    """把 ORM 行摊平成渲染层的输入。

    **纯函数**：不碰 Session、不发查询。这样"ORM 行怎么变成报告数据"可以直接
    单测（用 ``SimpleNamespace`` 造行即可），不必为了测它起一个 MySQL。
    """
    return ReportData(
        task=ReportTask(
            task_id=task.id,
            status=task.status,
            current_stage=task.current_stage,
            created_at=task.created_at,
        ),
        contract=ReportContract(
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
        file=ReportFile(
            file_id=contract_file.id,
            file_name=contract_file.file_name,
            # 与上传响应、P11 工作台同一口径：表里只有扩展名，类型码由同一个函数还原
            file_type=type_code_for_extension(contract_file.file_ext) or contract_file.file_ext,
            sha256=contract_file.sha256,
            parse_status=contract_file.parse_status,
        ),
        metadata=tuple(
            ReportMetadataItem(
                field_key=item.field_key,
                field_label=item.field_label,
                field_value=item.field_value,
                value_type=item.value_type,
                extract_method=item.extract_method,
            )
            for item in metadata
        ),
        clauses=tuple(
            ReportClause(
                clause_id=clause.id,
                clause_no=clause.clause_no,
                clause_type=clause.clause_type,
                title=clause.title,
                text=clause.text,
            )
            for clause in clauses
        ),
        risks=tuple(
            ReportRisk(
                risk_id=risk.id,
                risk_code=risk.risk_code,
                risk_title=risk.risk_title,
                dimension=risk.dimension,
                risk_level=risk.risk_level,
                source=risk.source,
                reason=risk.reason,
                legal_basis=risk.legal_basis,
                # ⚠️ 语义：库里的 original_text 是**命中片段**（P10 落库时由 Agent 的
                # quote 映射而来），不是整段原文。报告按这个语义用
                original_text=risk.original_text,
                paragraph_index=risk.paragraph_index,
                clause_id=risk.clause_id,
                locator_type=risk.locator_type,
                # 人工复核三列（P13-4）：与 AI 字段取自**同一行**，`_load_risks`
                # 取的就是完整的 RiskItem，因此这里不需要多查一次
                review_status=risk.review_status,
                review_comment=risk.review_comment,
                reviewed_at=risk.reviewed_at,
            )
            for risk in risks
        ),
    )
