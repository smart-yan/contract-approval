"""合同文件接入编排（架构文档 §4.1、§17 P4）。

职责
----
把「一个上传请求」变成「Contract + ContractFile + ReviewTask 三条记录」。
**不做**解析、OCR、规则引擎、LLM —— 那些属于 P7 及之后。

两层幂等（互相独立，各自绑定一个已有的唯一约束）
--------------------------------------------
======================  ==========================================  ===============================
层                       回答的问题                                    物理保障
======================  ==========================================  ===============================
**文件层**              是不是同一个物理文件？                        ``contract_file.sha256`` UNIQUE
**任务层**              同一个 Contract + 同一个 File + 同一套审查     ``review_task.idempotency_key``
                        配置，是否已有任务？                          UNIQUE
======================  ==========================================  ===============================

* 文件层由 :func:`_find_existing_by_sha256` 判定，结果写入 ``IngestResult.reused``
* 任务层由 :func:`_ensure_task_in_session` 判定，结果写入 ``IngestResult.task_reused``

**任务层不看 status**：同一套审查配置就对应同一个任务（pending / blocked / completed 一视同仁）；
要重新审查必须换配置（新规则集版本 / 新 prompt 版本）⇒ 新键 ⇒ 新任务，旧任务原样留存（§6.1）。

事务边界（P4 的核心设计）
------------------------
文件系统与数据库**不是同一个事务**，因此绝不能把文件 IO 包在 DB 事务里。
本模块严格按下列顺序执行，任何一段 DB 事务都不跨越文件 IO：

```
1. 校验（纯本地，不碰 DB）
2. 流式算 SHA-256（线程池，不碰 DB）
3. 事务 T0：文件层命中检查 + 任务层 ensure   ── 命中 ⇒ 复用返回；**内部无文件 IO**
4. 事务 T1：创建 Contract → 取 contract_id   ── 短事务，立即 commit（仅文件未命中时）
5. 原子落盘 storage/uploads/{contract_id}/   ── 事务外，无 DB Session 在手（仅文件未命中时）
6. 事务 T2：ContractFile + ReviewTask + 回填 current_task_id ── 短事务（仅文件未命中时）
```

**为什么 T0 可以同时读文件层和写任务层**：它内部没有文件 IO，全程只碰数据库，
因此不存在"事务跨越文件系统"的问题。这也是"文件复用但新建任务"能够成立的原因 ——
新任务挂在**已有**的 Contract 上，不需要新建 Contract，也就不需要新的存储目录。

**为什么 T1 与 T2 要拆开**：§7.2 规定最终路径含 ``contract_id``，
必须先拿到自增主键才能确定文件路径；而文件移动又不能待在事务里。
拆成两个短事务是同时满足这两条约束的最小代价。

失败与补偿
----------
* T1 失败 → 临时文件在 ``finally`` 里清理
* 落盘失败 → 同上
* T2 失败 → **尝试删除已落盘文件**；删除再失败则记录 orphan 日志（不实现 GC）
* 并发同 sha256 导致唯一键冲突 → 见下面「并发」

并发（架构裁决 Q4）
-------------------
``contract_file.sha256`` 的 UNIQUE 约束是**最终一致性保障**。
两个请求带同一份文件同时进来时：两个都会过 T0 预查重、都建出自己的 Contract，
最终只有一个能插入 ContractFile。
输的一方捕获 ``IntegrityError`` → 事务已回滚 → **开新事务重新查询** →
查到即按"复用"返回。

⚠️ 这里不能用"捕获异常后在原事务里重查"：MySQL 默认 REPEATABLE READ，
原事务的快照早于对方提交，重查**查不到**。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ContractSource, ParseStatus, TaskStage, TaskStatus
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.models.contract import Contract, ContractFile
from app.db.models.review_task import ReviewTask
from app.db.models.rule import ReviewRuleSet
from app.db.session import session_scope
from app.storage.base import StorageBackend
from app.storage.local import get_storage
from app.utils.file_utils import FileTypeSpec, type_code_for_extension, validate_upload
from app.utils.hash_utils import build_idempotency_key, sha256_file

logger = get_logger(__name__)

#: P4 的"审查引擎版本"占位符。
#: §17.1 的幂等键公式含 ``prompt_version``，但提示词属 P10、P4 没有来源。
#: 架构裁决（Q3）：P4 用固定值 ``ingest-v1``，P10 接入提示词后替换为真实版本。
INGEST_ENGINE_VERSION = "ingest-v1"

#: 该合同类型下没有启用规则集时，幂等键中该段使用的标记值（架构裁决 Q3）。
#: **没有规则集不能阻止上传** —— 只是幂等键少一段输入。
RULE_SET_VERSION_NONE = "NONE"


# --------------------------------------------------------------------------- #
# T2 异常语义：区分「确定失败」与「结果不确定」
#
# 补偿删除的前提是**能确定事务没有提交**。这两种情况必须分开表达，
# 否则就会出现在"结果不确定"时误删已提交记录的风险 —— 那比留下垃圾更糟。
# --------------------------------------------------------------------------- #
class _T2DuplicateConflict(Exception):
    """T2 因唯一键冲突失败 —— 并发同 sha256 的**预期**路径，事务确定已回滚。"""


class _T2DeterministicFailure(Exception):
    """T2 在 commit **之前**失败 —— 事务确定已回滚，可以安全补偿。

    :attr:`cause` 保存原始异常，补偿完成后重新抛出它（保持对外错误语义不变）。
    """

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(str(cause))


# --------------------------------------------------------------------------- #
# 输入 / 输出
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ContractMeta:
    """上传请求里的合同业务字段（架构裁决 Q2 / Q7 的最小必填集）。

    ``source`` 与 ``status`` 不由客户端提供，由系统填写。
    """

    contract_no: str
    title: str
    contract_type: str
    our_party: str | None = None
    counterparty: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    sign_date: date | None = None
    effective_date: date | None = None
    expire_date: date | None = None
    dept: str | None = None


@dataclass(frozen=True, slots=True)
class IngestResult:
    """接入结果。字段与 ``app.schemas.contract.ContractIngestResponse`` 一一对应。"""

    contract_id: int
    contract_no: str
    title: str
    contract_type: str
    contract_status: str

    file_id: int
    filename: str
    file_size: int
    file_type: str
    sha256: str
    parse_status: str

    review_task_id: int
    task_status: str
    task_stage: str

    #: **文件层**幂等：是否复用了已有的 ContractFile（及其所属 Contract）
    reused: bool
    #: **任务层**幂等：是否复用了已有的 ReviewTask
    task_reused: bool


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
async def ingest_contract(
    *,
    temp_path: Path,
    filename: str,
    content_type: str | None,
    size: int,
    meta: ContractMeta,
    storage: StorageBackend | None = None,
) -> IngestResult:
    """把一个已落盘到临时位置的合同文件接入系统。

    :param temp_path: 上传中转文件；本函数负责在结束时清理它
    :param size: 实际写入的字节数（不信任 Content-Length）
    """
    backend = storage or get_storage()
    try:
        # ---- 1. 校验（纯本地）----
        spec = validate_upload(filename=filename, content_type=content_type, size=size, path=temp_path)

        # ---- 2. 流式哈希（阻塞 IO → 默认线程池，见 §1.3）----
        digest = await asyncio.to_thread(sha256_file, temp_path)

        logger.info(
            "开始接入合同文件",
            extra={
                "contract_no": meta.contract_no,
                "contract_type": meta.contract_type,
                "file_name": filename,
                "file_type": spec.code,
                "file_size": size,
                "sha256": digest,
            },
        )

        # ---- 3. T0：文件层命中检查 + 任务层 ensure（短事务，内部无文件 IO）----
        existing = await _reuse_existing_file(digest)
        if existing is not None:
            return existing

        # ---- 4. T1：创建 Contract（短事务，立即提交）----
        contract_id = await _create_contract(meta)

        # ---- 5. 原子落盘（事务外，手里没有 DB Session）----
        storage_key = backend.build_upload_key(
            contract_id=contract_id, sha256=digest, extension=spec.extension
        )
        try:
            await asyncio.to_thread(backend.save, key=storage_key, source=temp_path)
        except Exception:
            # 补偿场景 2：Contract 已 commit，但附件/任务尚未创建，且未向客户端返回成功
            # ⇒ 这个 Contract 确定是孤儿，可以安全清理
            await _cleanup_orphan_contract(contract_id=contract_id, reason="storage_write_failed")
            raise

        # ---- 6. T2：ContractFile + ReviewTask（短事务）----
        try:
            result = await _create_file_and_task(
                contract_id=contract_id,
                storage_key=storage_key,
                digest=digest,
                spec=spec,
                filename=filename,
                size=size,
            )
        except _T2DuplicateConflict:
            # 补偿场景 1：并发同 sha256，T2 确定未提交，且已确认竞争者存在
            return await _resolve_concurrent_duplicate(
                digest=digest, storage_key=storage_key, backend=backend, contract_id=contract_id
            )
        except _T2DeterministicFailure as failure:
            # 补偿场景 3：T2 在 commit **之前**失败 —— 事务确定已回滚
            await _delete_orphan(backend, storage_key, digest, reason="t2_failed", contract_id=contract_id)
            await _cleanup_orphan_contract(contract_id=contract_id, reason="t2_failed")
            raise failure.cause from None
        except Exception:
            # ⚠️ 未包装的异常只可能来自 session_scope 的 commit/rollback 阶段，
            # 即"**提交结果不确定**"（数据库可能已经 COMMIT，但连接在拿到结果前断了）。
            # 此时禁止删除任何东西 —— 删掉一个其实已经提交成功的记录，比留下垃圾更糟。
            logger.warning(
                "T2 提交结果不确定，保留已落盘文件与合同记录以便后续协调",
                extra={"contract_id": contract_id, "sha256": digest, "reason": "commit_outcome_uncertain"},
            )
            raise

        logger.info(
            "合同文件接入完成",
            extra={
                "contract_id": result.contract_id,
                "file_id": result.file_id,
                "review_task_id": result.review_task_id,
                "sha256": digest,
                "reused": False,
            },
        )
        return result

    finally:
        # 落盘成功后 temp_path 已不存在（os.replace 移走了），missing_ok 使其成为 no-op
        await _discard_temp(temp_path)


# --------------------------------------------------------------------------- #
# 各事务段
# --------------------------------------------------------------------------- #
async def _find_existing_by_sha256(digest: str) -> tuple[Contract, ContractFile] | None:
    """**文件层**：按 sha256 查已有的合同与附件。**独立短事务**（并发重查时也复用它）。

    它只回答"是不是同一个物理文件"，**不碰 ReviewTask** ——
    任务层复用完全由 :func:`_ensure_task_in_session` 决定。

    返回的 ORM 实例在事务关闭后仍然可用：sessionmaker 配了
    ``expire_on_commit=False``，且本项目的模型没有任何 ``relationship()``
    （不存在懒加载），因此不会出现 DetachedInstanceError。
    """
    async with session_scope() as session:
        contract_file = (
            await session.execute(select(ContractFile).where(ContractFile.sha256 == digest))
        ).scalar_one_or_none()
        if contract_file is None:
            return None

        contract = await session.get(Contract, contract_file.contract_id)
        if contract is None:
            # 附件存在却没有合同，说明库里已有不一致状态，不能静默继续
            logger.warning(
                "附件存在但缺少关联的合同",
                extra={"file_id": contract_file.id, "sha256": digest},
            )
            raise AppError(
                code=ErrorCode.INTERNAL_ERROR,
                message="附件已存在，但关联的合同缺失",
            )

        return contract, contract_file


async def _ensure_task_in_session(
    session: AsyncSession,
    *,
    contract_id: int,
    file_id: int,
    file_sha256: str,
    contract_type: str,
) -> tuple[ReviewTask, bool]:
    """**任务层幂等的唯一实现点**：确保"这个 Contract + 这个 File + 这套审查配置"有任务。

    幂等键沿用 §17.1 的公式：

    ``sha256(contract_id ‖ file_sha256 ‖ rule_set_version ‖ prompt_version)``

    * **命中** → 复用该任务，**:attr:`task_reused` 为 True**，**不改它的 status**
    * **未命中** → 新建任务（``pending`` / ``UPLOADED``），返回 False

    ⚠️ **status 不参与判断。** 同一套审查配置就该对应同一个任务，
    无论它现在是 ``pending`` / ``parsing`` / ``reviewing`` / ``blocked`` / ``completed``。
    ``completed`` 是终态的含义是"**不能改它**"，而不是"不能返回它"；
    要重新审查必须**改变审查配置**（新规则集版本 / 新 prompt 版本）⇒ 天然得到新键 ⇒ 新任务，
    旧任务按 §6.1「历史任务全留存」原样保留。

    :param contract_type: **必须是实际 Contract 的类型**。复用已有文件时，
        它可能与本次请求传入的 ``meta.contract_type`` 不同 —— 那也应当以已有合同为准，
        否则会算出与既有任务不一致的键。

    ⚠️ 本函数**不做并发保护**：并发的正确性由 ``review_task.idempotency_key``
    的 UNIQUE 约束兜底，调用方必须处理 ``IntegrityError``。
    """
    rule_set_version = await _active_rule_set_version(session, contract_type)
    key = build_idempotency_key(contract_id, file_sha256, rule_set_version, INGEST_ENGINE_VERSION)

    existing = (
        await session.execute(select(ReviewTask).where(ReviewTask.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True

    task = ReviewTask(
        contract_id=contract_id,
        file_id=file_id,
        status=TaskStatus.PENDING.value,
        current_stage=TaskStage.UPLOADED.value,
        idempotency_key=key,
        # progress / priority / version / retry_count / max_retry 使用模型默认值
    )
    session.add(task)
    await session.flush()
    return task, False


async def _reuse_existing_file(digest: str) -> IngestResult | None:
    """文件层命中后的完整处理：复用 Contract/ContractFile，并确保任务层幂等。

    **没有文件 IO** —— 因此失败时既没有已落盘文件、也没有本请求创建的记录需要补偿，
    不需要任何孤儿清理。
    """
    existing = await _find_existing_by_sha256(digest)
    if existing is None:
        return None

    contract, contract_file = existing
    logger.info(
        "命中 sha256 去重，复用已有合同/附件",
        extra={"contract_id": contract.id, "file_id": contract_file.id, "sha256": digest},
    )
    return await _reuse_file_with_task(contract, contract_file)


async def _reuse_file_with_task(contract: Contract, contract_file: ContractFile) -> IngestResult:
    """在已复用的 Contract/ContractFile 上确保任务存在，并处理任务层并发。

    ``review_task.idempotency_key`` 的 UNIQUE 约束是**最终并发保障**：
    两个请求可能同时算出同一个键、同时查不到、同时尝试插入，只有一个能成功。
    输的一方事务已回滚，**开新事务重查**即可命中对方那一行
    （REPEATABLE READ 下原事务的快照看不到它）。
    """
    try:
        return await _reuse_file_in_session(contract, contract_file)
    except IntegrityError:
        logger.warning(
            "任务级幂等键并发冲突，开新事务重查",
            extra={
                "contract_id": contract.id,
                "file_id": contract_file.id,
                "sha256": contract_file.sha256,
            },
        )
        return await _reuse_file_in_session(contract, contract_file)


async def _reuse_file_in_session(contract: Contract, contract_file: ContractFile) -> IngestResult:
    """复用路径的实际读写：**单个短事务，内部无文件 IO**。"""
    async with session_scope() as session:
        task, task_reused = await _ensure_task_in_session(
            session,
            contract_id=contract.id,
            file_id=contract_file.id,
            file_sha256=contract_file.sha256,
            contract_type=contract.contract_type,
        )

        if not task_reused:
            # 只有**新建**任务才回填：复用旧任务绝不能让 current_task_id 倒退
            managed_contract = await session.get(Contract, contract.id)
            if managed_contract is not None:
                managed_contract.current_task_id = task.id
                await session.flush()

        logger.info(
            "复用已有合同/附件完成",
            extra={
                "contract_id": contract.id,
                "file_id": contract_file.id,
                "review_task_id": task.id,
                "task_reused": task_reused,
                "sha256": contract_file.sha256,
            },
        )
        return _build_result(contract, contract_file, task, file_reused=True, task_reused=task_reused)


async def _create_contract(meta: ContractMeta) -> int:
    """T1：创建合同，返回自增 id。事务在 ``session_scope`` 退出时提交。"""
    async with session_scope() as session:
        contract = Contract(
            contract_no=meta.contract_no,
            title=meta.title,
            contract_type=meta.contract_type,
            our_party=meta.our_party,
            counterparty=meta.counterparty,
            amount=meta.amount,
            currency=meta.currency,
            sign_date=meta.sign_date,
            effective_date=meta.effective_date,
            expire_date=meta.expire_date,
            dept=meta.dept,
            # 系统填写：来源固定为上传；状态跟随即将创建的任务
            source=ContractSource.UPLOAD.value,
            status=TaskStatus.PENDING.value,
            # applicant_id / approval_instance_id / current_task_id 在 T2 或后续阶段回填
            current_task_id=None,
        )
        session.add(contract)
        try:
            await session.flush()
        except IntegrityError:
            # contract_no 有 UNIQUE 约束
            raise AppError(
                code=ErrorCode.CONFLICT,
                message=f"合同编号已存在：{meta.contract_no}",
                details={"contract_no": meta.contract_no},
            ) from None
        return contract.id


async def _create_file_and_task(
    *,
    contract_id: int,
    storage_key: str,
    digest: str,
    spec: FileTypeSpec,
    filename: str,
    size: int,
) -> IngestResult:
    """T2：创建附件与审查任务，并回填合同的 current_task_id。

    异常被刻意分成三路，因为**补偿清理的可行性取决于事务是否确定未提交**：

    * ``_T2DuplicateConflict`` → 并发同 sha256 的**预期**路径，事务确定已回滚。
      它由 :func:`_write_file_and_task` 在 ``ContractFile`` 插入处**就地**抛出 ——
      那里才能确定冲突来自 ``contract_file.sha256`` 而不是 ``review_task.idempotency_key``。
    * 块内其它异常 → ``_T2DeterministicFailure``：事务**确定**已回滚，可补偿
    * 未被包装的异常 → 只可能来自 ``session_scope`` 的 commit/rollback 阶段，
      即提交结果**不确定**，禁止补偿

    ⚠️ 这里**刻意不再笼统捕获 ``IntegrityError``**：那条路径会把 ReviewTask 的
    幂等键冲突误判成"文件并发冲突"，进而错误地走复用分支。
    """
    async with session_scope() as session:
        try:
            return await _write_file_and_task(
                session,
                contract_id=contract_id,
                storage_key=storage_key,
                digest=digest,
                spec=spec,
                filename=filename,
                size=size,
            )
        except _T2DuplicateConflict:
            # 文件层并发冲突：语义明确，原样上抛
            raise
        except Exception as exc:
            # 异常发生在 commit 之前 ⇒ session_scope 会回滚 ⇒ 结果确定
            raise _T2DeterministicFailure(exc) from exc


async def _write_file_and_task(
    session: AsyncSession,
    *,
    contract_id: int,
    storage_key: str,
    digest: str,
    spec: FileTypeSpec,
    filename: str,
    size: int,
) -> IngestResult:
    """T2 的实际写入逻辑（异常语义由调用方 :func:`_create_file_and_task` 负责包装）。

    这里的 ``contract_id`` 是 T1 **刚创建**的，因此幂等键必然未命中 ——
    本路径上 :func:`_ensure_task_in_session` 实际总是新建任务，
    但仍然走同一个 helper，保证两条路径的任务创建逻辑只有一份实现。
    """
    contract_file = ContractFile(
        contract_id=contract_id,
        file_name=filename,
        file_ext=spec.extension,
        file_size=size,
        sha256=digest,
        storage_path=storage_key,
        is_scanned=False,
        # P4 只置为待解析；实际解析状态流转属 P7
        parse_status=ParseStatus.PENDING.value,
    )
    session.add(contract_file)
    try:
        await session.flush()
    except IntegrityError as exc:
        # 就地判定：这个 flush 只可能撞 contract_file.sha256 的唯一约束。
        # 在此处转换，才能与下面 ReviewTask 的幂等键冲突区分开。
        raise _T2DuplicateConflict() from exc

    contract = await session.get(Contract, contract_id)
    if contract is None:  # pragma: no cover - 同一请求内刚创建，理论上不可能
        raise AppError(code=ErrorCode.INTERNAL_ERROR, message="合同在事务内意外丢失")

    # 用**真实 Contract** 的 contract_type 取规则集版本（不是请求里的 meta）
    task, _task_reused = await _ensure_task_in_session(
        session,
        contract_id=contract_id,
        file_id=contract_file.id,
        file_sha256=digest,
        contract_type=contract.contract_type,
    )
    contract.current_task_id = task.id
    await session.flush()

    return _build_result(contract, contract_file, task, file_reused=False, task_reused=False)


async def _active_rule_set_version(session: AsyncSession, contract_type: str) -> str:
    """取该合同类型下启用规则集的版本；没有则返回 ``NONE``。

    ⚠️ **没有规则集不能阻止上传**（架构裁决 Q3）—— 只是幂等键少一段输入。
    """
    stmt = (
        select(ReviewRuleSet.version)
        .where(
            ReviewRuleSet.contract_type == contract_type,
            ReviewRuleSet.is_active.is_(True),
        )
        .order_by(ReviewRuleSet.id.desc())
        .limit(1)
    )
    version = (await session.execute(stmt)).scalar_one_or_none()
    return version or RULE_SET_VERSION_NONE


# --------------------------------------------------------------------------- #
# 并发与补偿
# --------------------------------------------------------------------------- #
async def _resolve_concurrent_duplicate(
    *,
    digest: str,
    storage_key: str,
    backend: StorageBackend,
    contract_id: int,
) -> IngestResult:
    """处理"另一个请求先插入了同一 sha256"。

    此时外层事务已由 ``session_scope`` 回滚，因此这里开的是**新事务** ——
    新快照才能看到对方已提交的行（RR 隔离级别下原事务是看不到的）。
    """
    winner = await _find_existing_by_sha256(digest)
    if winner is None:
        # 唯一键冲突了却查不到记录：属于真正的异常，不能假装复用。
        # 此时竞争者的存在性**无法确认**，因此按"结果不确定"处理：只清文件，不删 Contract。
        await _delete_orphan(backend, storage_key, digest, reason="integrity_error_without_existing")
        logger.warning(
            "唯一约束冲突但未找到既有记录，保留本次合同记录以便协调",
            extra={
                "contract_id": contract_id,
                "sha256": digest,
                "reason": "conflict_without_winner",
                "cleanup_result": "skipped_winner_unknown",
                "orphan_contract_id": contract_id,
            },
        )
        raise AppError(
            code=ErrorCode.INTERNAL_ERROR,
            message="文件唯一约束冲突，但未找到既有记录",
        )

    winner_contract, winner_file = winner

    # 补偿场景 1：竞争者已确认存在，本请求的 Contract 确定是孤儿
    await _delete_orphan(backend, storage_key, digest, reason="concurrent_duplicate")
    await _cleanup_orphan_contract(contract_id=contract_id, reason="concurrent_duplicate")

    logger.warning(
        "并发重复上传：已回退本次写入并复用胜出方的记录",
        extra={
            "orphan_contract_id": contract_id,
            "winner_contract_id": winner_contract.id,
            "file_id": winner_file.id,
            "sha256": digest,
        },
    )
    return await _reuse_file_with_task(winner_contract, winner_file)


async def _delete_orphan(
    backend: StorageBackend,
    storage_key: str,
    digest: str,
    *,
    reason: str,
    contract_id: int | None = None,
) -> None:
    """删除已落盘但不会有 DB 记录引用的文件。

    删除失败**只记 orphan 日志、不抛异常** —— 清理失败不能掩盖主流程真正的错误，
    但必须留下可追踪的痕迹（P4 不实现垃圾回收器）。
    """
    try:
        deleted = await asyncio.to_thread(backend.delete, storage_key)
    except Exception as exc:  # noqa: BLE001 - 清理阶段不应再抛
        logger.warning(
            "孤儿文件清理失败，需人工处理",
            extra={
                "reason": reason,
                "contract_id": contract_id,
                "sha256": digest,
                "error_type": type(exc).__name__,
            },
        )
        return

    logger.warning(
        "已清理孤儿文件" if deleted else "孤儿文件已不存在，无需清理",
        extra={"reason": reason, "contract_id": contract_id, "sha256": digest},
    )

    # 文件删掉后，竞争失败方留下的 uploads/{contract_id}/ 往往变成空目录。
    # 只删"确认已经为空"的那一层，不做递归、不做 GC。
    await _prune_empty_parent_dir(backend, storage_key, reason=reason, contract_id=contract_id)


async def _prune_empty_parent_dir(
    backend: StorageBackend,
    storage_key: str,
    *,
    reason: str,
    contract_id: int | None,
) -> None:
    """尽力删除 key 所在的、**已确认为空**的目录（如 ``uploads/{contract_id}/``）。

    三条安全约束：
    1. 只在目录确实为空时删除（有内容一律不动）
    2. 只删 key 的直接父目录，不递归、不越出存储根
    3. **任何失败都吞掉并结构化记录** —— 清理是尽力而为，绝不影响主流程
    """
    try:
        target = await asyncio.to_thread(backend.resolve, storage_key)
        parent = target.parent
        if not await asyncio.to_thread(parent.is_dir):
            return
        # next(...) 为 None ⇒ 目录为空
        if await asyncio.to_thread(lambda: next(parent.iterdir(), None)) is not None:
            return
        await asyncio.to_thread(parent.rmdir)
    except Exception as exc:  # noqa: BLE001 - 清理失败不得影响主流程
        logger.warning(
            "空目录清理失败（已忽略）",
            extra={
                "reason": reason,
                "contract_id": contract_id,
                "error_type": type(exc).__name__,
            },
        )
        return

    logger.info("已清理空目录", extra={"reason": reason, "contract_id": contract_id})


async def _cleanup_orphan_contract(*, contract_id: int, reason: str) -> str:
    """条件式补偿删除本请求刚创建的孤儿 Contract（架构裁决：方案 B）。

    **调用前提**：调用方必须能确定 T2 未提交（见 :class:`_T2DeterministicFailure`
    与 :class:`_T2DuplicateConflict` 的语义）。**结果不确定时禁止调用本函数。**

    删除前逐条确认，任何一条不满足就放弃删除并记日志：

    1. ``current_task_id IS NULL`` —— T2 没成功回填
    2. 没有 ``contract_file`` 引用它
    3. 没有 ``review_task`` 引用它

    使用**独立短事务**，绝不复用已经失败的那个事务。

    :return: 清理结果（写入结构化日志的 ``cleanup_result``）
    """
    try:
        async with session_scope() as session:
            contract = await session.get(Contract, contract_id)
            if contract is None:
                outcome = "skipped_not_found"
            elif contract.current_task_id is not None:
                # T2 其实成功过（或已被别的路径回填）—— 绝不能删
                outcome = "skipped_task_link_present"
            elif await session.scalar(
                select(ContractFile.id).where(ContractFile.contract_id == contract_id).limit(1)
            ):
                outcome = "skipped_file_exists"
            elif await session.scalar(
                select(ReviewTask.id).where(ReviewTask.contract_id == contract_id).limit(1)
            ):
                outcome = "skipped_task_exists"
            else:
                await session.execute(
                    delete(Contract).where(Contract.id == contract_id, Contract.current_task_id.is_(None))
                )
                outcome = "deleted"
    except Exception as exc:  # noqa: BLE001 - 清理失败不能掩盖主流程错误
        logger.warning(
            "孤儿 Contract 补偿清理失败，需人工处理",
            extra={
                "contract_id": contract_id,
                "reason": reason,
                "cleanup_result": "failed",
                "error_type": type(exc).__name__,
                "orphan_contract_id": contract_id,
            },
        )
        return "failed"

    logger.warning(
        "孤儿 Contract 补偿清理",
        extra={
            "contract_id": contract_id,
            "reason": reason,
            "cleanup_result": outcome,
            # 没删成就是"确认为孤儿但仍在库里"，需要人工介入
            "orphan_contract_id": None if outcome == "deleted" else contract_id,
        },
    )
    return outcome


async def _discard_temp(temp_path: Path) -> None:
    """清理上传中转文件。失败只记日志。"""
    try:
        await asyncio.to_thread(temp_path.unlink, True)  # missing_ok=True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "上传中转文件清理失败",
            extra={"error_type": type(exc).__name__},
        )


# --------------------------------------------------------------------------- #
# 结果组装
# --------------------------------------------------------------------------- #
def _build_result(
    contract: Contract,
    contract_file: ContractFile,
    task: ReviewTask,
    *,
    file_reused: bool,
    task_reused: bool,
) -> IngestResult:
    return IngestResult(
        contract_id=contract.id,
        contract_no=contract.contract_no,
        title=contract.title,
        contract_type=contract.contract_type,
        contract_status=contract.status,
        file_id=contract_file.id,
        filename=contract_file.file_name,
        file_size=contract_file.file_size,
        # 文件类型码没有单独存列，由扩展名反推（与上传时的判定同源）
        file_type=type_code_for_extension(contract_file.file_ext) or contract_file.file_ext,
        sha256=contract_file.sha256,
        parse_status=contract_file.parse_status,
        review_task_id=task.id,
        task_status=task.status,
        task_stage=task.current_stage,
        reused=file_reused,
        task_reused=task_reused,
    )
