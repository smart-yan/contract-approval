"""孤儿 Contract 补偿清理的回归测试（**需要真实 MySQL**）。

为什么必须在 MySQL 上测
-----------------------
被测的核心是"唯一约束冲突 → 回滚 → **新事务**重查"这条链路。
它依赖 MySQL 的 REPEATABLE READ 快照语义与真实的外键/唯一约束 ——
SQLite 上跑不出同样的行为，结论没有意义。

三个补偿场景（架构裁决方案 B）
------------------------------
1. 并发 sha256 去重失败 → 删除本请求刚创建的 Contract
2. 文件原子落位失败     → 删除本请求刚创建的 Contract
3. T2 commit 前确定性失败 → 删除本请求刚创建的 Contract
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pymysql
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.models.contract import Contract, ContractFile
from app.db.models.review_task import ReviewTask
from app.db.session import dispose_engine, session_scope
from app.services import contract_ingest
from app.services.contract_ingest import ContractMeta, ingest_contract
from app.storage.local import LocalStorageBackend

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _db_available() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.mysql_password.get_secret_value():
        return False, ".env 中未配置 MYSQL_PASSWORD"
    try:
        conn = pymysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            database=settings.mysql_db,
            charset="utf8mb4",
            connect_timeout=3,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}（MySQL 服务未启动或库不存在？）"
    else:
        conn.close()
        return True, ""


_AVAILABLE, _REASON = _db_available()

pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=f"MySQL 不可用：{_REASON}")

#: 本模块使用的合同编号前缀，用于收尾清理
PREFIX = "IT-RACE-"


def _make_docx() -> bytes:
    """最小合法 DOCX（ZIP 容器）。P4 不解析内容，只校验魔数。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return buf.getvalue()


def _write_temp(storage: LocalStorageBackend, payload: bytes) -> Path:
    temp_path = storage.new_temp_path(suffix=".docx")
    temp_path.write_bytes(payload)
    return temp_path


def _meta(contract_no: str) -> ContractMeta:
    return ContractMeta(
        contract_no=contract_no, title=f"并发测试合同 {contract_no}", contract_type="PURCHASE"
    )


async def _purge() -> None:
    """删除本模块产生的数据库记录。

    删除顺序必须与外键依赖相反（review_task → contract_file → contract）：
    FK 是 RESTRICT，顺序错了数据库会直接拒绝。

    文件无需手动清理 —— 每个用例的存储根目录都在 pytest 的 ``tmp_path`` 下，自动回收。
    """
    async with session_scope() as session:
        contract_ids = list(
            (
                await session.execute(select(Contract.id).where(Contract.contract_no.like(f"{PREFIX}%")))
            ).scalars()
        )
        if not contract_ids:
            return
        await session.execute(delete(ReviewTask).where(ReviewTask.contract_id.in_(contract_ids)))
        await session.execute(delete(ContractFile).where(ContractFile.contract_id.in_(contract_ids)))
        await session.execute(delete(Contract).where(Contract.id.in_(contract_ids)))


@pytest.fixture(autouse=True)
async def _cleanup() -> Iterator[None]:
    yield
    await _purge()
    await dispose_engine()


# --------------------------------------------------------------------------- #
# 场景 1：并发 sha256 去重失败 —— 必须不留孤儿 Contract（本阶段要求的核心回归）
# --------------------------------------------------------------------------- #
async def test_concurrent_duplicate_leaves_no_orphan_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发同 sha256 时，输的一方必须回退干净：不留 Contract、不留文件。

    并发场景的真实复现方式：把 T0 预查重打上补丁，让它**第一次返回 None**
    （模拟"我方检查时，对方的行还没提交"），之后恢复正常。
    这样流程会完整走到 T2 并真的撞上 ``contract_file.sha256`` 唯一约束。
    """
    storage = LocalStorageBackend(tmp_path / "storage")
    payload = _make_docx()

    # ---- 1. 先造出"胜出方" ----
    winner = await ingest_contract(
        temp_path=_write_temp(storage, payload),
        filename="winner.docx",
        content_type=DOCX_MIME,
        size=len(payload),
        meta=_meta(f"{PREFIX}WINNER"),
        storage=storage,
    )
    assert winner.reused is False, "胜出方应当是新建"

    # ---- 2. 让后续请求的 T0 看不到已存在的那一行 ----
    original_find = contract_ingest._find_existing_by_sha256
    calls = {"count": 0}

    async def blinded_first_lookup(digest: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return None  # 模拟"检查时对方尚未提交"
        return await original_find(digest)

    monkeypatch.setattr(contract_ingest, "_find_existing_by_sha256", blinded_first_lookup)

    # ---- 3. 同一份内容、不同合同编号 → 走到 T2 才会撞唯一约束 ----
    loser = await ingest_contract(
        temp_path=_write_temp(storage, payload),
        filename="loser.docx",
        content_type=DOCX_MIME,
        size=len(payload),
        meta=_meta(f"{PREFIX}LOSER"),
        storage=storage,
    )

    # ---- 4. 断言：复用了胜出方，且输的一方什么痕迹都没留下 ----
    assert loser.reused is True
    assert loser.task_reused is True, "同一套审查配置 ⇒ 任务层也应当命中胜出方的任务"
    assert loser.contract_id == winner.contract_id
    assert loser.file_id == winner.file_id
    assert loser.review_task_id == winner.review_task_id
    assert calls["count"] >= 2, "并发重查路径没有被触发，测试前提不成立"

    async with session_scope() as session:
        contract_nos = set((await session.execute(select(Contract.contract_no))).scalars())
        file_count = len(list((await session.execute(select(ContractFile.id))).scalars()))
        task_count = len(list((await session.execute(select(ReviewTask.id))).scalars()))

    assert f"{PREFIX}WINNER" in contract_nos
    assert f"{PREFIX}LOSER" not in contract_nos, "并发失败的一方留下了孤儿 Contract"

    assert file_count == 1, "合同附件出现重复"
    assert task_count == 1, "审查任务出现重复"

    # 文件层面：胜出方的文件在，输的一方的目录里不能有文件
    uploads = storage.root / "uploads"
    for directory in uploads.iterdir():
        if not directory.is_dir():
            continue
        files = list(directory.iterdir())
        if directory.name == str(winner.contract_id):
            assert len(files) == 1, "胜出方的文件应当保留"
        else:
            assert files == [], f"孤儿目录里仍有文件：{directory}"


# --------------------------------------------------------------------------- #
# 场景 2：文件落位失败
# --------------------------------------------------------------------------- #
async def test_storage_write_failure_cleans_up_orphan_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """落盘失败时：Contract 已 commit 但附件/任务未创建 ⇒ 必须清理掉这个 Contract。"""

    class _FailingStorage(LocalStorageBackend):
        def save(self, *, key: str, source: Path) -> None:
            raise AppError(code=ErrorCode.STORAGE_UNAVAILABLE, message="模拟落盘失败")

    storage = _FailingStorage(tmp_path / "storage")
    payload = _make_docx()
    contract_no = f"{PREFIX}STORAGE-FAIL"

    with pytest.raises(AppError) as exc_info:
        await ingest_contract(
            temp_path=_write_temp(LocalStorageBackend(tmp_path / "storage"), payload),
            filename="f.docx",
            content_type=DOCX_MIME,
            size=len(payload),
            meta=_meta(contract_no),
            storage=storage,
        )

    assert exc_info.value.code == ErrorCode.STORAGE_UNAVAILABLE

    async with session_scope() as session:
        remaining = (
            (await session.execute(select(Contract.id).where(Contract.contract_no == contract_no)))
            .scalars()
            .all()
        )
    assert remaining == [], "落盘失败后留下了孤儿 Contract"


# --------------------------------------------------------------------------- #
# 场景 3：T2 在 commit 之前确定性失败
# --------------------------------------------------------------------------- #
async def test_t2_deterministic_failure_cleans_up_orphan_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2 块内抛异常 ⇒ 事务确定回滚 ⇒ 必须清理 Contract，并原样抛出业务异常。"""

    async def _boom(*args, **kwargs):
        raise RuntimeError("模拟 T2 确定性失败")

    monkeypatch.setattr(contract_ingest, "_write_file_and_task", _boom)

    storage = LocalStorageBackend(tmp_path / "storage")
    payload = _make_docx()
    contract_no = f"{PREFIX}T2-FAIL"

    with pytest.raises(RuntimeError, match="模拟 T2 确定性失败"):
        await ingest_contract(
            temp_path=_write_temp(storage, payload),
            filename="t2.docx",
            content_type=DOCX_MIME,
            size=len(payload),
            meta=_meta(contract_no),
            storage=storage,
        )

    async with session_scope() as session:
        remaining = (
            (await session.execute(select(Contract.id).where(Contract.contract_no == contract_no)))
            .scalars()
            .all()
        )
        file_count = len(list((await session.execute(select(ContractFile.id))).scalars()))
    assert remaining == [], "T2 确定性失败后留下了孤儿 Contract"
    assert file_count == 0, "T2 失败后不应留下附件记录"


# --------------------------------------------------------------------------- #
# 条件守卫：有子记录时绝不删除
# --------------------------------------------------------------------------- #
async def test_cleanup_skips_when_task_link_present(tmp_path: Path) -> None:
    """保护性断言：``current_task_id`` 已回填的 Contract **绝不会**被补偿删除。"""
    storage = LocalStorageBackend(tmp_path / "storage")
    payload = _make_docx()
    contract_no = f"{PREFIX}GUARD"

    result = await ingest_contract(
        temp_path=_write_temp(storage, payload),
        filename="guard.docx",
        content_type=DOCX_MIME,
        size=len(payload),
        meta=_meta(contract_no),
        storage=storage,
    )

    # 即便显式调用清理，也必须因为"已有关联任务"而拒绝删除
    outcome = await contract_ingest._cleanup_orphan_contract(
        contract_id=result.contract_id, reason="test_guard"
    )
    assert outcome == "skipped_task_link_present"

    async with session_scope() as session:
        still_there = (
            (await session.execute(select(Contract.id).where(Contract.id == result.contract_id)))
            .scalars()
            .all()
        )
    assert still_there == [result.contract_id], "有子记录的 Contract 被误删"


# --------------------------------------------------------------------------- #
# 任务层幂等的两个 IntegrityError 分支
#
# `review_task.idempotency_key` 的 UNIQUE 是任务层的物理保障，但它的冲突
# **必须与 `contract_file.sha256` 的冲突区分开** —— 两者的补偿动作完全不同：
#   文件层冲突 → 整体复用胜出方（_T2DuplicateConflict）
#   任务层冲突 → 复用场景下重查；新建场景下按确定性失败补偿（_T2DeterministicFailure）
# --------------------------------------------------------------------------- #
async def test_task_key_conflict_is_retried_in_a_fresh_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复用路径上的任务键冲突：必须开新事务重查并复用，而不是把整次请求判失败。

    模拟方式：让 `_ensure_task_in_session` 第一次抛 IntegrityError
    （等价于"另一个请求刚提交了同一个键"），之后恢复正常。
    """
    storage = LocalStorageBackend(tmp_path / "storage")
    payload = _make_docx()

    winner = await ingest_contract(
        temp_path=_write_temp(storage, payload),
        filename="winner.docx",
        content_type=DOCX_MIME,
        size=len(payload),
        meta=_meta(f"{PREFIX}TASK-A"),
        storage=storage,
    )

    original = contract_ingest._ensure_task_in_session
    calls = {"count": 0}

    async def flaky_ensure(session, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise IntegrityError(
                "INSERT INTO review_task ...", {}, Exception("uq_review_task_idempotency_key")
            )
        return await original(session, **kwargs)

    monkeypatch.setattr(contract_ingest, "_ensure_task_in_session", flaky_ensure)

    loser = await ingest_contract(
        temp_path=_write_temp(storage, payload),
        filename="loser.docx",
        content_type=DOCX_MIME,
        size=len(payload),
        meta=_meta(f"{PREFIX}TASK-B"),
        storage=storage,
    )

    assert calls["count"] == 2, "任务键冲突后必须重查一次"
    assert loser.reused is True
    assert loser.task_reused is True, "重查应当命中胜出方已有的任务"
    assert loser.contract_id == winner.contract_id
    assert loser.review_task_id == winner.review_task_id

    async with session_scope() as session:
        task_count = len(
            list(
                (
                    await session.execute(
                        select(ReviewTask.id).where(ReviewTask.contract_id == winner.contract_id)
                    )
                ).scalars()
            )
        )
    assert task_count == 1, "重查不得产生第二个任务"


async def test_task_insert_failure_on_fresh_path_is_not_treated_as_file_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新建路径上的任务键冲突：必须走"确定性失败"补偿。

    这是本轮修复的要点 —— 以前笼统的 ``except IntegrityError`` 会把它误判成
    "文件并发冲突"，进而去查一个并不存在的胜出方，最后抛 INTERNAL_ERROR
    并**把这次创建的合同留在库里**（孤儿）。
    """
    storage = LocalStorageBackend(tmp_path / "storage")
    payload = _make_docx()
    contract_no = f"{PREFIX}TASK-C"

    async def always_conflict(session, **kwargs):
        raise IntegrityError("INSERT INTO review_task ...", {}, Exception("uq_review_task_idempotency_key"))

    monkeypatch.setattr(contract_ingest, "_ensure_task_in_session", always_conflict)

    with pytest.raises(IntegrityError):
        await ingest_contract(
            temp_path=_write_temp(storage, payload),
            filename="c.docx",
            content_type=DOCX_MIME,
            size=len(payload),
            meta=_meta(contract_no),
            storage=storage,
        )

    async with session_scope() as session:
        remaining = (
            (await session.execute(select(Contract.id).where(Contract.contract_no == contract_no)))
            .scalars()
            .all()
        )
        file_count = len(list((await session.execute(select(ContractFile.id))).scalars()))
    assert remaining == [], "确定性失败后留下了孤儿 Contract"
    assert file_count == 0, "T2 失败后不应留下附件记录"

    uploads = storage.root / "uploads"
    leftovers = [p for d in uploads.iterdir() if d.is_dir() for p in d.iterdir()]
    assert leftovers == [], f"孤儿文件未被清理：{leftovers}"
