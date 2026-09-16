"""Mock 审批域基础数据的集成测试（**需要真实 MySQL**，P15-3a）。

两部分，都用真库验证：

* **列与约束**：``approval_comment.idempotency_key`` 存在、可空、
  且受 ``UNIQUE(instance_id, idempotency_key)`` 约束 —— 这些只写在 migration 里，
  不看真库是验证不了的（``tests/integration/test_query_indexes_migration.py``
  同一条理由）
* **seed 脚本**：以**子进程**方式跑 ``scripts/seed_mock_approval.py``（人怎么用就怎么测），
  再回读库确认：4 张确定性审批单、可重复执行不重复建、能把合同绑上、不抢已有绑定

⚠️ seed 产生的 ``MOCK-AP-00x`` 审批单是**预置数据**，不是测试夹具 —— 本模块**不清除**它们
（后续 P15 步骤要引用）。本模块只清理自己造的 ``IT-MA-`` 前缀数据。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pymysql
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
SAMPLES_DIR = BACKEND_DIR.parent / "samples"
GOLDEN_SAMPLE = SAMPLES_DIR / "采购合同-风险版.docx"

#: 本模块自造数据的合同号前缀
PREFIX = "IT-MA-"

#: §7.2 给**回写产生的评论**规定的 ``source`` 值。22 个字符 ——
#: 正是 P3 的 ``varchar(16)`` 存不下、而回写链路必然要写的那个值。
WRITEBACK_SOURCE = "CONTRACT_REVIEW_SYSTEM"

#: §7.2 的另一种来源：**人工**评论（没有外部请求身份，因此没有幂等键）。
MANUAL_SOURCE = "MANUAL"

#: §14.4 点名的 4 张审批单（与 seed 脚本一致的期望值）
EXPECTED_INSTANCE_NOS = ("MOCK-AP-001", "MOCK-AP-002", "MOCK-AP-003", "MOCK-AP-004")


def _connect() -> pymysql.connections.Connection:
    from app.core.config import get_settings

    settings = get_settings()
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password.get_secret_value(),
        database=settings.mysql_db,
        charset="utf8mb4",
        connect_timeout=5,
        autocommit=True,
    )


def _db_available() -> tuple[bool, str]:
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.mysql_password.get_secret_value():
        return False, ".env 中未配置 MYSQL_PASSWORD"
    try:
        _connect().close()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}（MySQL 服务未启动或库不存在？）"
    return True, ""


_AVAILABLE, _REASON = _db_available()

pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=f"MySQL 不可用：{_REASON}")


def _query(sql: str, params: tuple = ()) -> list[tuple]:
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        conn.close()


def _execute(sql: str, params: tuple = ()) -> int:
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.lastrowid or cursor.rowcount
    finally:
        conn.close()


def _scalar(sql: str, params: tuple = ()):
    rows = _query(sql, params)
    return rows[0][0] if rows else None


def _purge_test_data() -> None:
    """只清本模块自造的 ``IT-MA-`` 数据（审批单先于合同删：评论引用审批单）。"""
    instance_ids = [
        row[0]
        for row in _query("SELECT id FROM approval_instance WHERE instance_no LIKE %s", (f"{PREFIX}%",))
    ]
    if instance_ids:
        ph = ",".join(["%s"] * len(instance_ids))
        _query(f"DELETE FROM approval_comment WHERE instance_id IN ({ph})", tuple(instance_ids))
        _query(f"DELETE FROM approval_instance WHERE id IN ({ph})", tuple(instance_ids))

    contract_ids = [
        row[0] for row in _query("SELECT id FROM contract WHERE contract_no LIKE %s", (f"{PREFIX}%",))
    ]
    if contract_ids:
        ph = ",".join(["%s"] * len(contract_ids))
        task_ids = [
            row[0]
            for row in _query(f"SELECT id FROM review_task WHERE contract_id IN ({ph})", tuple(contract_ids))
        ]
        if task_ids:
            task_ph = ",".join(["%s"] * len(task_ids))
            _query(f"DELETE FROM writeback_record WHERE task_id IN ({task_ph})", tuple(task_ids))
            _query(f"DELETE FROM review_task WHERE id IN ({task_ph})", tuple(task_ids))
        _query(f"DELETE FROM contract_file WHERE contract_id IN ({ph})", tuple(contract_ids))
        _query(f"DELETE FROM contract WHERE id IN ({ph})", tuple(contract_ids))


def _snapshot_golden_binding() -> tuple[int, int | None] | None:
    """记录黄金样例所属合同当前的绑定，测试结束后原样还回去。

    ⚠️ 那份合同**不是本模块的数据**（它是真实接入流程留下的），因此本模块
    既不删它、也不该把测试期间改出来的绑定留给它。
    """
    contract_id = _golden_contract_id()
    if contract_id is None:
        return None
    return contract_id, _scalar("SELECT approval_instance_id FROM contract WHERE id = %s", (contract_id,))


def _restore_golden_binding(snapshot: tuple[int, int | None] | None) -> None:
    if snapshot is None:
        return
    contract_id, previous = snapshot
    _execute("UPDATE contract SET approval_instance_id = %s WHERE id = %s", (previous, contract_id))


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    _purge_test_data()
    snapshot = _snapshot_golden_binding()
    yield
    _restore_golden_binding(snapshot)
    _purge_test_data()


def _run_seed() -> subprocess.CompletedProcess[str]:
    """以**子进程**方式跑 seed（与人工执行完全一致）。"""
    return subprocess.run(
        [sys.executable, "scripts/seed_mock_approval.py"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,  # 返回码由调用方断言 —— 失败时要能读到完整输出，而不是抛 CalledProcessError
    )


def _insert_test_instance(instance_no: str) -> int:
    return _execute(
        "INSERT INTO approval_instance "
        "(instance_no, title, applicant, dept, amount, status, contract_id, created_at, updated_at) "
        "VALUES (%s, '测试审批单', '测试', NULL, NULL, 'PENDING', NULL, NOW(3), NOW(3))",
        (instance_no,),
    )


def _insert_comment(
    instance_id: int,
    *,
    key: str | None,
    content: str = "评论内容",
    source: str = WRITEBACK_SOURCE,
) -> None:
    """插一条评论。默认用**回写来源**（§7.2 的 ``CONTRACT_REVIEW_SYSTEM``）。

    ⚠️ 这个值有 **22 个字符**，而 P3 建的列原本只有 16 —— 回写链路一旦真的要写评论
    就会撞上 MySQL 1406。本模块因此**真的用它**来插数据：列宽一旦退回 16，
    这些用例会立刻失败（这正是那个阻塞问题的回归守卫）。
    """
    _execute(
        "INSERT INTO approval_comment "
        "(instance_id, author, content, comment_type, source, external_id, idempotency_key, "
        " created_at, updated_at) "
        "VALUES (%s, '系统', %s, 'SYSTEM_REVIEW', %s, NULL, %s, NOW(3), NOW(3))",
        (instance_id, content, source, key),
    )


def _golden_contract_id() -> int | None:
    """持有黄金样例那份文件的合同（``contract_file.sha256`` 全局唯一 ⇒ 至多一个）。"""
    return _scalar("SELECT contract_id FROM contract_file WHERE sha256 = %s", (_sha256_of(GOLDEN_SAMPLE),))


def _golden_contract() -> int:
    """拿到黄金样例对应的合同：库里已有就用它（那是真实流程留下的），没有才造一个。

    ⚠️ ``contract_file.sha256`` 是**全局 UNIQUE**，同一份文件不可能挂两个合同 ——
    所以"再造一个"只在第一次跑（或库被清过）时可行。
    """
    existing = _golden_contract_id()
    if existing is not None:
        return existing
    contract_id, _ = _insert_contract(f"{PREFIX}GOLDEN", sha256=_sha256_of(GOLDEN_SAMPLE))
    return contract_id


def _insert_contract(contract_no: str, *, sha256: str) -> tuple[int, int]:
    contract_id = _execute(
        "INSERT INTO contract "
        "(contract_no, title, contract_type, source, status, created_at, updated_at) "
        "VALUES (%s, '测试合同', 'PURCHASE', 'UPLOAD', 'PENDING', NOW(3), NOW(3))",
        (contract_no,),
    )
    file_id = _execute(
        "INSERT INTO contract_file "
        "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
        " created_at, updated_at) "
        "VALUES (%s, 'contract.docx', '.docx', 1024, %s, %s, 0, 'PARSED', NOW(3), NOW(3))",
        (contract_id, sha256, f"{contract_id}/contract.docx"),
    )
    return contract_id, file_id


# --------------------------------------------------------------------------- #
# 1 / 2 / 3 / 4：列、约束与幂等语义（真库）
# --------------------------------------------------------------------------- #
def test_the_idempotency_key_column_exists_and_is_nullable() -> None:
    """列必须存在且**可空** —— 人工评论没有"外部请求身份"这回事。"""
    rows = _query(
        "SELECT COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'approval_comment' "
        "AND COLUMN_NAME = 'idempotency_key'"
    )

    assert rows, "approval_comment.idempotency_key 不存在（migration 没跑到 head？）"
    assert rows[0][0].lower() == "varchar(64)"
    assert rows[0][1] == "YES", "人工评论没有幂等键，列必须可空"


def test_the_source_column_can_hold_the_documented_writeback_value() -> None:
    """**P15-3a 的阻塞项**：``source`` 必须装得下 §7.2 的
    ``CONTRACT_REVIEW_SYSTEM``（22 字符）—— 否则回写链路写评论必然 1406。

    列宽与"能不能真的写进去"一起断言：只查 information_schema 的话，
    某个中间件/字符集差异还能骗过断言；真的插一条再读回来才是最终判据。
    """
    rows = _query(
        "SELECT COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'approval_comment' AND COLUMN_NAME = 'source'"
    )

    assert rows, "approval_comment.source 不存在"
    assert rows[0][0].lower() == "varchar(32)"
    assert rows[0][1] == 32

    instance_id = _insert_test_instance(f"{PREFIX}SRC")
    _insert_comment(instance_id, key=None, content="回写评论", source=WRITEBACK_SOURCE)

    assert (
        _scalar("SELECT source FROM approval_comment WHERE instance_id = %s", (instance_id,))
        == WRITEBACK_SOURCE
    )


def test_a_manual_comment_still_fits_in_the_widened_column() -> None:
    """加宽没有把另一种来源挤掉：``MANUAL``（§7.2 的另半张取值表）照常可写可读。"""
    instance_id = _insert_test_instance(f"{PREFIX}MANUAL")
    _insert_comment(instance_id, key=None, content="人工评论", source=MANUAL_SOURCE)

    assert (
        _scalar("SELECT source FROM approval_comment WHERE instance_id = %s", (instance_id,)) == MANUAL_SOURCE
    )


def test_the_unique_constraint_still_works_after_widening() -> None:
    """加宽是**原地**变更，不该动到那条复合唯一约束。

    这里用**回写来源**插两条同键评论（正是真实回写会走到的那条路），
    第二条必须被数据库挡下。
    """
    instance_id = _insert_test_instance(f"{PREFIX}SRC-KEY")
    _insert_comment(instance_id, key="s" * 64, content="第一次回写")

    with pytest.raises(pymysql.err.IntegrityError):
        _insert_comment(instance_id, key="s" * 64, content="第二次回写")

    assert _scalar("SELECT COUNT(*) FROM approval_comment WHERE instance_id = %s", (instance_id,)) == 1


def test_the_unique_constraint_is_scoped_to_the_instance() -> None:
    rows = _query(
        "SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'approval_comment' "
        "AND NON_UNIQUE = 0 AND INDEX_NAME <> 'PRIMARY' "
        "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
    )

    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("uq_approval_comment_instance_id_idempotency_key", 1, "instance_id"),
        ("uq_approval_comment_instance_id_idempotency_key", 2, "idempotency_key"),
    ], "唯一键必须是 (instance_id, idempotency_key) 的复合唯一，且列顺序一致"


def test_the_same_key_cannot_appear_twice_on_one_instance() -> None:
    """**重复键返回既有评论不新建**在数据库层的落地：第二条根本插不进来。"""
    instance_id = _insert_test_instance(f"{PREFIX}KEY-1")
    _insert_comment(instance_id, key="k" * 64)

    with pytest.raises(pymysql.err.IntegrityError):
        _insert_comment(instance_id, key="k" * 64, content="同一把钥匙的第二条")

    assert _scalar("SELECT COUNT(*) FROM approval_comment WHERE instance_id = %s", (instance_id,)) == 1


def test_the_same_key_may_appear_on_two_instances() -> None:
    """幂等键是"外部**请求**的身份"，不是评论的全局标识 —— 两个审批单互不干扰。"""
    first = _insert_test_instance(f"{PREFIX}KEY-2A")
    second = _insert_test_instance(f"{PREFIX}KEY-2B")

    _insert_comment(first, key="k" * 64)
    _insert_comment(second, key="k" * 64)

    assert _scalar("SELECT COUNT(*) FROM approval_comment WHERE idempotency_key = %s", ("k" * 64,)) == 2


def test_manual_comments_without_a_key_are_not_restricted() -> None:
    """人工评论（没有外部请求身份）可以有很多条 —— MySQL 不把多个 NULL 视为冲突。

    这条语义是**刻意依赖**的（不是巧合）：如果列设成 NOT NULL，人工评论就被迫
    编一个假键，而那正是这次裁决否掉的做法。
    """
    instance_id = _insert_test_instance(f"{PREFIX}NULL-KEY")
    _insert_comment(instance_id, key=None, content="人工评论一")
    _insert_comment(instance_id, key=None, content="人工评论二")

    assert _scalar("SELECT COUNT(*) FROM approval_comment WHERE instance_id = %s", (instance_id,)) == 2


# --------------------------------------------------------------------------- #
# 5 / 6 / 7 / 8：seed 脚本
# --------------------------------------------------------------------------- #
def test_the_seed_creates_the_four_documented_instances() -> None:
    result = _run_seed()
    assert result.returncode == 0, f"seed 失败：{result.stdout}\n{result.stderr}"

    rows = _query(
        "SELECT instance_no, title, applicant, dept, amount, status FROM approval_instance "
        "WHERE instance_no IN ({}) ORDER BY instance_no".format(
            ",".join(["%s"] * len(EXPECTED_INSTANCE_NOS))
        ),
        EXPECTED_INSTANCE_NOS,
    )

    assert [row[0] for row in rows] == list(EXPECTED_INSTANCE_NOS)
    titles = {row[0]: row[1] for row in rows}
    assert titles["MOCK-AP-001"] == "软件采购合同"
    assert titles["MOCK-AP-003"] == "劳动合同"
    # 劳动合同不适用"合同金额"，如实留空而不是编一个数
    amounts = {row[0]: row[4] for row in rows}
    assert amounts["MOCK-AP-003"] is None


def test_the_seed_is_repeatable_and_creates_no_duplicate() -> None:
    first = _run_seed()
    assert first.returncode == 0, first.stdout

    ids_before = {
        row[0]: row[1]
        for row in _query(
            "SELECT instance_no, id FROM approval_instance WHERE instance_no IN ({})".format(
                ",".join(["%s"] * len(EXPECTED_INSTANCE_NOS))
            ),
            EXPECTED_INSTANCE_NOS,
        )
    }

    second = _run_seed()
    assert second.returncode == 0, second.stdout

    ids_after = {
        row[0]: row[1]
        for row in _query(
            "SELECT instance_no, id FROM approval_instance WHERE instance_no IN ({})".format(
                ",".join(["%s"] * len(EXPECTED_INSTANCE_NOS))
            ),
            EXPECTED_INSTANCE_NOS,
        )
    }

    assert ids_after == ids_before, "重复执行不得新建审批单（id 必须一模一样）"
    assert len(ids_after) == len(EXPECTED_INSTANCE_NOS)


def test_the_seed_binds_the_golden_contract() -> None:
    """绑定规则可复现：样例文件的 sha256 → 合同 → ``contract.approval_instance_id``。"""
    assert GOLDEN_SAMPLE.is_file(), f"缺少黄金样例：{GOLDEN_SAMPLE}"
    contract_id = _golden_contract()

    result = _run_seed()
    assert result.returncode == 0, result.stdout

    bound_to = _scalar("SELECT approval_instance_id FROM contract WHERE id = %s", (contract_id,))
    instance_id = _scalar("SELECT id FROM approval_instance WHERE instance_no = 'MOCK-AP-001'")
    assert bound_to == instance_id, "黄金样例对应的合同应绑定到 MOCK-AP-001（端到端主演示场景）"

    # ⚠️ 审批单**侧**的列刻意不填：那是带 FK 的列，填了会让合同删不掉
    assert _scalar("SELECT contract_id FROM approval_instance WHERE id = %s", (instance_id,)) is None, (
        "本步只写合同侧；审批单侧的 contract_id 带 FK，留给需要它的那一步"
    )


def test_the_seed_does_not_steal_an_existing_binding() -> None:
    contract_id = _golden_contract()
    other_instance = _insert_test_instance(f"{PREFIX}OTHER")
    _execute("UPDATE contract SET approval_instance_id = %s WHERE id = %s", (other_instance, contract_id))

    result = _run_seed()
    assert result.returncode == 0, result.stdout

    assert (
        _scalar("SELECT approval_instance_id FROM contract WHERE id = %s", (contract_id,)) == other_instance
    ), "合同已绑到别的审批单时，seed 不该覆盖它"


def test_the_seed_does_not_touch_writeback_records() -> None:
    """seed 只写 Mock 审批域，不碰 P15-1/P15-2 的数据。"""
    before = _scalar("SELECT COUNT(*) FROM writeback_record")

    result = _run_seed()
    assert result.returncode == 0, result.stdout

    assert _scalar("SELECT COUNT(*) FROM writeback_record") == before


def _sha256_of(path: Path) -> str:
    """样例文件的 SHA-256 —— 绑定规则就建立在它之上（见 seed 的 docstring）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()
