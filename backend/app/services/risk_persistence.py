"""审查风险持久化（P9-10）。

职责边界
-------
::

    Agent 的最终风险列表  →  整批校验 → 整批 INSERT → 任务状态更新
                            （Backend 只做持久化）

Backend 在这一层**不做任何智能判断**：不认识 LLM、不跑规则、不合并风险、
不判断维度是否合理。它只回答三个问题：

1. 这些风险**能不能**落库（外键解析得出来吗？有没有重复写入？）
2. 落库时那些**服务端才知道**的字段填什么（``locator_type`` / ``review_status``
   / ``rule_id`` / ``clause_id``）
3. 这次写入与任务状态更新是不是**一起**成功或一起失败

一次写入 = 一个事务
------------------
整批 INSERT 与 ``review_task`` 的状态更新处在**同一个** :func:`session_scope`
里。任何一步失败（校验不过 / INSERT 报错 / 状态更新报错）都会让整个
``async with`` 块抛出去，``session_scope`` 随即 rollback ——

**绝不会出现"风险写了一半"或"风险写进去了但任务还停在 pending"**。
半批风险比没有风险更糟：人工审核看到的是一份看起来完整、实则缺了几条的清单，
而且没有任何信号提示它不完整。

幂等：一个任务一次，两层门禁
----------------
``risks=[]`` **也是一次真实的审查结果**（"这次审查没有风险"）。因此"写过了"
不能只看有没有风险行 —— 空批次一行都不会写。

::

    第一层（阶段门禁）  task.current_stage == REVIEWED  →  409
    第二层（兜底）      该 task 已有 risk_item 行        →  409

两层都用 :attr:`~app.core.errors.ErrorCode.TASK_ALREADY_PERSISTED`（409），
调用方不需要区分"被哪一层拦下的"。

``current_stage`` 在这里是**阶段门禁**，不是新造的幂等键：它本来就是任务自身的
生命周期字段（§6.1），我们只是读它做个判断 —— **没有新增列、没有新增迁移、
没有第二个真相源**。它表达的是"该 ReviewTask 是否已完成本阶段的持久化"。

第二层仍然保留：任务阶段被外部改回 ``UPLOADED`` 却没有清掉风险行时，
它挡住"在人工复核过的数据上再写一遍"。

刻意**不**做"先删后插"覆盖：已落库的风险可能已经带有人工复核结果
（``review_status`` 不再是 ``PENDING``、``risk_suggestion`` 里可能有法务编辑过的
最终文本），覆盖会把它们**静默抹掉**。追加同样不行 —— 人工会看到两份一样的卡片。

并发：靠 ``review_task`` 的行锁串行化
--------------------------------
两层判据都是"先读后写"，因此**必须在同一个事务里先拿到任务行的排他锁**
（:func:`_load_task` 的 ``SELECT ... FOR UPDATE``）。没有那把锁，两个并发请求
会同时读到 ``UPLOADED`` + 零行，然后双双写入 —— 而 ``risk_item`` 除主键外
没有唯一约束，数据库层拦不住。

有了锁之后的顺序是确定的：

::

    请求 A：拿到行锁 → 门禁通过 → 写入 → current_stage=REVIEWED → COMMIT
    请求 B：阻塞在同一行锁上 …… A 提交 → 读到最新 REVIEWED → 409

（不需要迁移、不需要新列、不需要唯一约束、不需要分布式锁。）

任务状态：**只推进阶段，不动状态机**
--------------------------------
写入成功后**只更新** ``current_stage = REVIEWED``。

``status`` 与 ``finished_at`` **保持原样**（``pending`` / ``NULL``）。这不是遗漏，
是刻意的：

* 本阶段能表达的事实只有一句 —— **"Agent 已完成风险审查并已成功持久化"**，
  它对应 ``current_stage``
* ``status = COMPLETED`` 需要的迁移 ``PENDING → COMPLETED`` **不在
  §6.1 的 ``TASK_STATUS_TRANSITIONS`` 里**，而那套矩阵被声明为唯一权威。
  绕过它等于让状态机名存实亡
* 逐级走 ``PENDING → PARSING → REVIEWING → COMPLETED`` 同样不行 ——
  那是在**伪造生命周期历史**：这几个阶段在 Backend 侧从来没有真正发生过
* ``finished_at`` 是"任务结束时刻"。任务尚未结束就填上它，等于留下一个
  与 ``status`` 自相矛盾的时间戳

（"任务何时算结束、由谁推进状态机"是后续阶段的事 —— 例如评分与结论产出之后。）

⚠️ ``risk_level_final``（综合风险等级）与 ``conclusion``（审查结论）**刻意留空**：
它们是 §11.2 的**评分**结果，而评分器不在本阶段范围内（Agent 侧同样没有）。
用"最高等级"顺手填一个值，等于在这个字段里写下一个没人负责的结论。

服务端决定的字段
--------------
======================  ==================================================
``locator_type``        由所属附件的 ``file_type`` 派生（与
                        ``document_block.locator_type`` 同源，§10.4）
``review_status``       固定 ``PENDING``（AI 刚产出，尚未复核）
``rule_id``             Agent 只给 ``risk_code``，按**任务所属规则集**解析
``clause_id``           按 ``paragraph_index`` 落在哪个条款的段落区间解析
======================  ==================================================
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.constants import LocatorType, RiskReviewStatus, TaskStage
from app.core.errors import ConflictError, ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.contract import Contract, ContractFile
from app.db.models.document import Clause, DocumentBlock
from app.db.models.review_task import ReviewTask
from app.db.models.risk import RiskItem
from app.db.models.rule import ReviewRule
from app.db.session import session_scope
from app.schemas.risk import RiskItemCreate, RiskPersistResponse
from app.services.rule_catalog import _find_effective_rule_set
from app.utils.datetime_utils import utcnow

logger = get_logger(__name__)

#: 段落级定位的文件**扩展名**（小写，§10.4）：DOCX 有真实段号、没有真实页码。
#: 其余（PDF / 扫描件 / 图片）一律是 PAGE：它们有页码，段落是"页内第 N 个文本块"。
_PARAGRAPH_LOCATOR_TYPES: frozenset[str] = frozenset({"docx"})


async def persist_task_risks(task_id: int, risks: Sequence[RiskItemCreate]) -> RiskPersistResponse:
    """把一批风险**原子地**写入该任务，并把任务置为已审查。

    :raises NotFoundError: 任务不存在（``TASK_NOT_FOUND``）；
        或某个 ``risk_code`` 在该任务的规则集里找不到（``RULE_NOT_FOUND``）
    :raises ConflictError: 该任务已经写入过风险（``TASK_ALREADY_PERSISTED``）
    :raises ValidationError: 条款归属出现歧义（``VALIDATION_ERROR``）
    """
    async with session_scope() as session:
        task = await _load_task(session, task_id)

        # ---- 第一层门禁：该任务**这一阶段**是否已经持久化过 ----
        # ⚠️ 这一层不能省。空批次会过去，但**写的行数是 0** ——
        # 只靠下面的行数检查，``risks=[]`` 就会留下"已 REVIEWED 却还能再写"的缺口。
        # ``current_stage`` 在这里的角色是**阶段门禁**（"本阶段已完成"），
        # 不是一个新的独立幂等键：它本来就是任务自身的生命周期字段，
        # 我们只是把它读出来做判断，没有为幂等再造一列、也没加迁移。
        if task.current_stage == TaskStage.REVIEWED.value:
            logger.warning(
                "拒绝重复写入风险",
                extra={"task_id": task_id, "current_stage": task.current_stage},
            )
            raise ConflictError(
                f"任务 {task_id} 已处于 {TaskStage.REVIEWED.value} 阶段，本阶段的风险已持久化过"
                "（哪怕当时写入的是空批次），不允许重复写入或覆盖。如需重新审查，请新建审查任务",
                code=ErrorCode.TASK_ALREADY_PERSISTED,
                details={"task_id": task_id, "current_stage": task.current_stage},
            )

        # ---- 第二层保护：真的已经有风险行（不覆盖、不追加）----
        # 正常路径下第一层已经拦住了；这一层是兜底 —— 例如任务阶段被外部改回
        # UPLOADED 却没有清掉风险行时，仍然不许在人工复核过的数据上再写一遍。
        written = await session.scalar(
            select(func.count()).select_from(RiskItem).where(RiskItem.task_id == task_id)
        )
        if written:
            logger.warning(
                "拒绝重复写入风险",
                extra={"task_id": task_id, "existing_risk_count": written},
            )
            raise ConflictError(
                f"任务 {task_id} 已有 {written} 条风险，不允许重复写入或覆盖。如需重新审查，请新建审查任务",
                code=ErrorCode.TASK_ALREADY_PERSISTED,
                details={"task_id": task_id, "existing_risk_count": written},
            )

        # ---- 服务端才知道的字段：定位方式来自附件类型 ----
        locator_type = await _resolve_locator_type(session, task)
        contract_type = await _resolve_contract_type(session, task)

        # ---- 外键解析（解析不出来就整批失败，绝不静默写 NULL）----
        rule_id_by_code = await _resolve_rule_ids(session, contract_type, risks)
        clause_ranges = await _load_clause_ranges(session, task_id)

        now = utcnow()
        for risk in risks:
            session.add(
                RiskItem(
                    task_id=task_id,
                    contract_id=task.contract_id,
                    clause_id=_resolve_clause_id(clause_ranges, risk.paragraph_index),
                    rule_id=rule_id_by_code.get(risk.risk_code) if risk.risk_code else None,
                    risk_code=risk.risk_code,
                    risk_title=risk.risk_title,
                    dimension=risk.dimension,
                    risk_level=risk.risk_level.value,
                    source=risk.source.value,
                    reason=risk.reason,
                    legal_basis=risk.legal_basis,
                    # ⚠️ 语义映射：Agent 的 quote（命中片段）才是这一列要的东西。
                    # AgentRiskItem.original_text 是"证据所在的段落原文"，不进这里
                    original_text=risk.quote,
                    paragraph_index=risk.paragraph_index,
                    locator_type=locator_type,
                    anchor_method=risk.anchor_method.value if risk.anchor_method else None,
                    # 服务端固定：AI 刚产出，尚未人工复核。客户端无权指定
                    review_status=RiskReviewStatus.PENDING.value,
                    created_at=now,
                    updated_at=now,
                )
            )

        # flush 让 INSERT 真正发出去 —— 约束冲突在这里就会炸，而不是等到 commit
        await session.flush()

        # ---- 同事务更新任务阶段 ----
        # ⚠️ **只动 current_stage**：本阶段表达的是"Agent 已完成风险审查并持久化"，
        # 不是"任务已结束"。``status`` 与 ``finished_at`` 保持原样，理由见模块 docstring。
        task.current_stage = TaskStage.REVIEWED.value
        task.updated_at = now

        logger.info(
            "风险持久化完成",
            extra={
                "task_id": task_id,
                "contract_id": task.contract_id,
                "persisted": len(risks),
                "task_status": task.status,
            },
        )
        return RiskPersistResponse(
            task_id=task_id,
            persisted=len(risks),
            task_status=task.status,
            task_stage=task.current_stage,
        )


# --------------------------------------------------------------------------- #
# 读取与解析
# --------------------------------------------------------------------------- #
async def _load_task(session: AsyncSession, task_id: int) -> ReviewTask:
    """取任务，并**对它的行加锁**（``SELECT ... FOR UPDATE``）。

    这一行是并发门禁的基石。本事务里接下来的判断（阶段是否已完成、有没有风险行）
    都是"先读后写"，如果读的是**快照**，两个并发请求可能同时读到
    ``UPLOADED`` + 零行，然后双双写入 —— 数据库层没有任何东西能拦住它
    （``risk_item`` 除主键外没有唯一约束）。

    ``FOR UPDATE`` 是**当前读**而不是快照读：第二个事务会**阻塞**在这里，
    等第一个事务提交后再读，于是它读到的是**最新的** ``current_stage`` ——
    也就是 ``REVIEWED``，直接 409。判据从"读一个可能过期的快照"变成
    "拿到锁之后再读"。

    项目里已有同样的用法：§1.4 抢任务用的就是同一张表上的
    ``SELECT ... FOR UPDATE SKIP LOCKED``。本事务内没有任何 IO（不调 LLM / OCR，
    只有几条查询），因此持锁时间极短。

    取不到任务时**同样持有锁语义**：`scalar_one_or_none()` 走的是同一把行锁，
    不存在"先取不到、后又被别人建出来"的窗口（任务不会被并发创建 —— 它由上传阶段建好）。
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


async def _resolve_contract_type(session: AsyncSession, task: ReviewTask) -> str:
    """任务的合同类型 —— 规则集按它选取。"""
    contract_type = await session.scalar(
        select(Contract.contract_type).where(Contract.id == task.contract_id)
    )
    if contract_type is None:
        # 任务的 contract_id 是 NOT NULL FK，查不到说明数据被破坏，不能当成"没有合同类型"
        raise NotFoundError(
            f"任务 {task.id} 关联的合同 {task.contract_id} 不存在",
            code=ErrorCode.CONTRACT_NOT_FOUND,
            details={"task_id": task.id, "contract_id": task.contract_id},
        )
    return contract_type


async def _resolve_locator_type(session: AsyncSession, task: ReviewTask) -> str:
    """由所属附件的**扩展名**派生定位方式（§10.4）。

    DOCX → ``PARAGRAPH``（有真实段号、无真实页码）；其余（PDF / 图片）→ ``PAGE``。
    **不让客户端传这个值**：它是附件的事实，与 ``document_block.locator_type`` 同源，
    两边一旦不一致，前端按 ``locator_type`` 选择的定位文案就会与实际坐标矛盾。

    ⚠️ 取的是 ``contract_file.file_ext``（如 ``docx``）—— 表里**没有** ``file_type`` 列，
    那个值是上传响应时从扩展名派生出来的展示口径，持久化这里必须回到原始列。
    """
    file_ext = await session.scalar(select(ContractFile.file_ext).where(ContractFile.id == task.file_id))
    if file_ext is None:
        raise NotFoundError(
            f"任务 {task.id} 关联的附件 {task.file_id} 不存在",
            code=ErrorCode.FILE_NOT_FOUND,
            details={"task_id": task.id, "file_id": task.file_id},
        )
    return (
        LocatorType.PARAGRAPH.value
        if _normalize_ext(file_ext) in _PARAGRAPH_LOCATOR_TYPES
        else LocatorType.PAGE.value
    )


def _normalize_ext(file_ext: str) -> str:
    """扩展名归一化：``.DOCX`` 与 ``docx`` 是同一件事。"""
    return file_ext.strip().lower().lstrip(".")


async def _resolve_rule_ids(
    session: AsyncSession, contract_type: str, risks: Sequence[RiskItemCreate]
) -> dict[str, int]:
    """``risk_code`` → ``review_rule.id``。

    规则集按**与上传时同一口径**选取（该合同类型下 ``is_active`` 且 id 最大的一套）——
    直接复用 :func:`app.services.rule_catalog._find_effective_rule_set`，
    而不是在这里再写一遍选取逻辑：两处口径一旦漂移，Agent 评估用的规则
    与这里解析外键用的规则就会不是同一套。

    解析不出来时**整批失败**（``RULE_NOT_FOUND``），绝不静默写 NULL ——
    ``rule_id`` 为空会被读成"这条风险不来自任何规则"，而事实是"我们找不到那条规则"。
    两者在下游（统计、按规则回溯）里的含义完全不同。

    ⚠️ 匹配时**不过滤 ``is_active``**：规则编码是精确匹配，且这条规则在审查当时
    确实启用过（Agent 的规则快照只含启用规则）。管理员在审查与落库之间禁用了它，
    不应该让这一批风险全部写不进去。
    """
    codes = {risk.risk_code for risk in risks if risk.risk_code}
    if not codes:
        return {}

    rule_set = await _find_effective_rule_set(session, contract_type)
    if rule_set is None:
        raise NotFoundError(
            f"合同类型 {contract_type} 当前没有启用的规则集，无法解析风险里的规则编码",
            code=ErrorCode.RULE_NOT_FOUND,
            details={"contract_type": contract_type, "risk_codes": sorted(codes)},
        )

    rows = await session.execute(
        select(ReviewRule.rule_code, ReviewRule.id).where(
            ReviewRule.rule_set_id == rule_set.id,
            ReviewRule.rule_code.in_(codes),
        )
    )
    mapping = {code: rule_id for code, rule_id in rows.all()}

    missing = sorted(codes - mapping.keys())
    if missing:
        raise NotFoundError(
            f"规则集 {rule_set.version} 里找不到这些规则编码：{'、'.join(missing)}",
            code=ErrorCode.RULE_NOT_FOUND,
            details={"rule_set_id": rule_set.id, "unknown_rule_codes": missing},
        )
    return mapping


@dataclass(frozen=True, slots=True)
class _ClauseRange:
    """一个条款覆盖的段落区间。"""

    clause_id: int
    paragraph_start: int
    paragraph_end: int


async def _load_clause_ranges(session: AsyncSession, task_id: int) -> list[_ClauseRange]:
    """该任务下各条款覆盖的段落区间。

    ``clause`` 只记起止 **block**（``start_block_id`` / ``end_block_id``），
    段落号在 ``document_block`` 上，因此需要把两个 block 各 join 一次取其
    ``paragraph_index``。取两者的 min/max 作为区间：同一文件里
    ``order_index`` 与 ``paragraph_index`` 同序（§7.2 的坐标约定），
    中间的 block 必然落在该区间内。
    """
    start_block = aliased(DocumentBlock)
    end_block = aliased(DocumentBlock)
    stmt = (
        select(Clause.id, start_block.paragraph_index, end_block.paragraph_index)
        .join(start_block, Clause.start_block_id == start_block.id)
        .join(end_block, Clause.end_block_id == end_block.id)
        .where(Clause.task_id == task_id)
    )
    rows = (await session.execute(stmt)).all()
    return [_ClauseRange(cid, min(a, b), max(a, b)) for cid, a, b in rows]


def _resolve_clause_id(ranges: Sequence[_ClauseRange], paragraph_index: int) -> int | None:
    """段落号落在哪个条款里。

    三种结局：

    ==================  ====================================================
    恰好一个条款覆盖     取它的 id
    没有任何条款覆盖     ``None`` —— ``clause_id`` 本来就 nullable（§7.2：
                        MISSING 类风险没有对应条款），写 NULL 是**事实**
    多个条款同时覆盖     **整批拒绝**（``VALIDATION_ERROR``）—— 覆盖区间本该
                        互不相交，同时覆盖说明条款切分或坐标数据有问题，
                        此时挑任何一个都是**猜**
    ==================  ====================================================

    ⚠️ 当前 ``clause`` / ``document_block`` **还没有任何写入路径**（条款切分在
    Agent 侧，落库尚未实现），因此实际上总会走到"没有任何条款覆盖" → NULL。
    这不是异常，也不会让整批失败 —— 等条款落库接上后这段逻辑才开始真正生效。
    """
    hits = [r.clause_id for r in ranges if r.paragraph_start <= paragraph_index <= r.paragraph_end]
    if len(hits) > 1:
        raise ValidationError(
            f"段落 {paragraph_index} 同时落在 {len(hits)} 个条款区间内（{hits}），"
            "无法确定它属于哪个条款，拒绝整批写入",
            code=ErrorCode.VALIDATION_ERROR,
            details={"paragraph_index": paragraph_index, "candidate_clause_ids": hits},
        )
    return hits[0] if hits else None


__all__ = ["persist_task_risks"]
