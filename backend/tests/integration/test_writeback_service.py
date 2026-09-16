"""回写服务的集成测试（**需要真实 MySQL**，P15-2）。

这里钉住的是**持久化语义**，其中几件在 mock 里根本测不出来：

* **门禁**：只有 ``current_stage == REVIEWED`` 才允许回写（409 ``WRITEBACK_NOT_READY``）
* **幂等**：``UNIQUE(idempotency_key)`` + "同内容复用 / 内容变了新建"
* **状态机**：``writing → success / failed``、``failed → writing``（重试，``attempt+1``），
  以及非法迁移被拒（``INVALID_STATE_TRANSITION``）
* **并发兜底**：两个并发"首次发起"里输的那一方，必须**开新事务重查**后按既有记录返回，
  而不是抛 500

隔离方式：自造 ``IT-WB2-`` 前缀的合同与审批单，收尾按外键反序删除。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pymysql
import pytest

from app.core.constants import WritebackStatus
from app.core.errors import ConflictError, ErrorCode, NotFoundError, ValidationError
from app.schemas.writeback import WritebackStartResponse
from app.services.report_query import load_report_data
from app.services.writeback import finish_writeback, start_writeback
from app.services.writeback_render import render_writeback

PREFIX = "IT-WB2-"


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


# --------------------------------------------------------------------------- #
# 数据库小工具（与其它集成测试同一套写法）
# --------------------------------------------------------------------------- #
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


def _purge() -> None:
    contract_ids = [
        row[0] for row in _query("SELECT id FROM contract WHERE contract_no LIKE %s", (f"{PREFIX}%",))
    ]
    if contract_ids:
        placeholders = ",".join(["%s"] * len(contract_ids))
        task_ids = [
            row[0]
            for row in _query(
                f"SELECT id FROM review_task WHERE contract_id IN ({placeholders})", tuple(contract_ids)
            )
        ]
        if task_ids:
            task_ph = ",".join(["%s"] * len(task_ids))
            # ⚠️ writeback_record 引用 approval_instance，必须在审批单之前删
            _query(f"DELETE FROM writeback_record WHERE task_id IN ({task_ph})", tuple(task_ids))
            for table in ("risk_item", "contract_metadata", "clause"):
                _query(f"DELETE FROM {table} WHERE task_id IN ({task_ph})", tuple(task_ids))
            _query(f"DELETE FROM review_task WHERE id IN ({task_ph})", tuple(task_ids))
        _query(f"DELETE FROM document_block WHERE contract_id IN ({placeholders})", tuple(contract_ids))
        _query(f"DELETE FROM contract_file WHERE contract_id IN ({placeholders})", tuple(contract_ids))
        _query(f"DELETE FROM contract WHERE id IN ({placeholders})", tuple(contract_ids))

    instance_ids = [
        row[0]
        for row in _query("SELECT id FROM approval_instance WHERE instance_no LIKE %s", (f"{PREFIX}%",))
    ]
    if instance_ids:
        instance_ph = ",".join(["%s"] * len(instance_ids))
        _query(f"DELETE FROM approval_comment WHERE instance_id IN ({instance_ph})", tuple(instance_ids))
        _query(f"DELETE FROM approval_instance WHERE id IN ({instance_ph})", tuple(instance_ids))


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    _purge()
    yield
    _purge()


async def _dispose() -> None:
    from app.db.session import dispose_engine

    await dispose_engine()


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scene:
    contract_id: int
    file_id: int
    task_id: int
    instance_id: int
    risk_id: int


def _insert_instance() -> int:
    return _execute(
        "INSERT INTO approval_instance "
        "(instance_no, title, applicant, dept, amount, status, contract_id, created_at, updated_at) "
        "VALUES (%s, '回写测试审批单', '张三', '法务部', 1000.00, 'PENDING', NULL, NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:12]}",),
    )


def _insert_contract() -> int:
    return _execute(
        "INSERT INTO contract "
        "(contract_no, title, contract_type, source, status, our_party, counterparty, amount, currency, "
        " sign_date, dept, created_at, updated_at) "
        "VALUES (%s, '回写测试合同', 'PURCHASE', 'UPLOAD', 'PENDING', '某某科技', '乙方公司', "
        "        1234.50, 'CNY', '2026-09-15', '法务部', NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:12]}",),
    )


def _insert_file(contract_id: int) -> int:
    return _execute(
        "INSERT INTO contract_file "
        "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
        " created_at, updated_at) "
        "VALUES (%s, 'contract.docx', '.docx', 1024, %s, %s, 0, 'PARSED', NOW(3), NOW(3))",
        (contract_id, uuid.uuid4().hex + uuid.uuid4().hex, f"{contract_id}/contract.docx"),
    )


def _insert_task(contract_id: int, file_id: int, *, stage: str = "REVIEWED") -> int:
    return _execute(
        "INSERT INTO review_task "
        "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
        " max_retry, idempotency_key, created_at, updated_at) "
        "VALUES (%s, %s, 'pending', %s, 0, 0, 0, 0, 3, %s, NOW(3), NOW(3))",
        (contract_id, file_id, stage, uuid.uuid4().hex + uuid.uuid4().hex),
    )


def _insert_risk(
    task_id: int,
    contract_id: int,
    *,
    risk_title: str = "知识产权归属相对方",
    review_status: str = "PENDING",
    review_comment: str | None = None,
) -> int:
    return _execute(
        "INSERT INTO risk_item "
        "(task_id, contract_id, clause_id, risk_code, risk_title, dimension, risk_level, source, "
        " reason, legal_basis, original_text, paragraph_index, locator_type, review_status, "
        " review_comment, created_at, updated_at) "
        "VALUES (%s, %s, NULL, 'IP_OWNER_SUPPLIER_001', %s, '知识产权', 'HIGH', 'RULE', '成因', '依据', "
        "        '命中片段', 23, 'PARAGRAPH', %s, %s, NOW(3), NOW(3))",
        (task_id, contract_id, risk_title, review_status, review_comment),
    )


def _build_scene(*, stage: str = "REVIEWED", risk: bool = True) -> Scene:
    instance_id = _insert_instance()
    contract_id = _insert_contract()
    file_id = _insert_file(contract_id)
    task_id = _insert_task(contract_id, file_id, stage=stage)
    risk_id = _insert_risk(task_id, contract_id) if risk else 0
    return Scene(
        contract_id=contract_id,
        file_id=file_id,
        task_id=task_id,
        instance_id=instance_id,
        risk_id=risk_id,
    )


def _records(task_id: int) -> list[tuple]:
    return _query(
        "SELECT id, status, attempt, content_hash, idempotency_key, error_msg, external_comment_id "
        "FROM writeback_record WHERE task_id = %s ORDER BY id",
        (task_id,),
    )


async def _start(scene: Scene, status_code: str | None = None) -> WritebackStartResponse:
    return await start_writeback(scene.task_id, scene.instance_id)


def _run(coro):
    """在同步测试里跑一个协程（每个用例自建事件循环，跑完释放引擎）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(_dispose())
        loop.close()


# --------------------------------------------------------------------------- #
# 1. 门禁
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stage", ["UPLOADED", "PARSED", "CLAUSED"])
def test_a_task_that_is_not_reviewed_cannot_be_written_back(stage: str) -> None:
    scene = _build_scene(stage=stage)

    with pytest.raises(ConflictError) as caught:
        _run(_start(scene))

    assert caught.value.code == ErrorCode.WRITEBACK_NOT_READY
    assert caught.value.http_status == 409
    assert _records(scene.task_id) == [], "门禁没过就不该留下任何回写记录"


def test_a_missing_task_is_reported_as_task_not_found() -> None:
    scene = _build_scene()

    with pytest.raises(NotFoundError) as caught:
        _run(start_writeback(999_999_999, scene.instance_id))

    assert caught.value.code == ErrorCode.TASK_NOT_FOUND


def test_a_pending_risk_does_not_block_the_writeback() -> None:
    """**P15-2 的关键结论**：风险是否已人工复核**不是**门禁。

    依据 §6.3（人工复核"不是流程的一环"、不参与任何综合裁决）与 P13 的冻结语义
    （复核不推进 ``current_stage``）。身体里如实写着"待复核"，由点击写回的人判断。
    """
    scene = _build_scene()  # 风险保持 PENDING
    assert _scalar("SELECT review_status FROM risk_item WHERE id = %s", (scene.risk_id,)) == "PENDING"

    response = _run(_start(scene))

    assert response.status == WritebackStatus.WRITING.value
    assert "| 人工复核 | 待复核 |" in response.content_md


# --------------------------------------------------------------------------- #
# 2. 正常生成记录
# --------------------------------------------------------------------------- #
def test_a_start_creates_a_writing_record() -> None:
    scene = _build_scene()

    response = _run(_start(scene))

    row = _query(
        "SELECT task_id, contract_id, approval_instance_id, status, attempt, content_md, content_hash, "
        "       idempotency_key, started_at, finished_at, operator_id, external_comment_id, error_msg "
        "FROM writeback_record WHERE id = %s",
        (response.record_id,),
    )[0]
    (
        task_id,
        contract_id,
        instance_id,
        status,
        attempt,
        content_md,
        content_hash,
        key,
        started_at,
        finished_at,
        operator_id,
        external_comment_id,
        error_msg,
    ) = row

    assert task_id == scene.task_id
    assert contract_id == scene.contract_id, "contract_id 取自**本任务的合同**，不是请求参数"
    assert instance_id == scene.instance_id
    assert status == WritebackStatus.WRITING.value
    assert attempt == 1
    assert started_at is not None, "发起时刻要落库"
    assert finished_at is None, "还没结束就不该有结束时刻"
    assert operator_id is None, "没有登录体系 —— 不伪造操作人"
    assert external_comment_id is None and error_msg is None
    # 响应里的正文与库里那份快照逐字节相同
    assert response.content_md == content_md
    assert response.content_hash == content_hash
    assert response.idempotency_key == key
    assert response.started is True


def test_the_persisted_snapshot_equals_the_renderer_output() -> None:
    """P15-1 的契约没有被破坏：库里那份正文就是 renderer 对同一份投影的输出。"""
    scene = _build_scene()

    response = _run(_start(scene))

    data = _run(load_report_data(scene.task_id))
    expected = render_writeback(data.task, data.contract, data.risks)

    assert response.content_md == expected.content_md
    assert response.content_hash == expected.content_hash
    assert response.idempotency_key == expected.idempotency_key
    assert response.content_md == _scalar(
        "SELECT content_md FROM writeback_record WHERE id = %s", (response.record_id,)
    )


def test_an_unknown_approval_instance_is_refused() -> None:
    scene = _build_scene()

    with pytest.raises(ValidationError) as caught:
        _run(start_writeback(scene.task_id, 999_999_999))

    assert caught.value.code == ErrorCode.VALIDATION_ERROR
    assert _records(scene.task_id) == []


# --------------------------------------------------------------------------- #
# 3 / 4. 幂等：同内容复用，内容变了新建
# --------------------------------------------------------------------------- #
def test_the_same_content_reuses_the_record_instead_of_starting_again() -> None:
    """还在 ``writing`` 时再点一次：**不新发起**（矩阵里没有 writing → writing）。"""
    scene = _build_scene()

    first = _run(_start(scene))
    second = _run(_start(scene))

    assert second.record_id == first.record_id
    assert second.started is False, "调用方据此不要再往审批系统发一次评论"
    assert second.attempt == 1, "没有新发起一次尝试，attempt 不该增加"
    assert len(_records(scene.task_id)) == 1


def test_a_successful_writeback_refuses_a_repeat() -> None:
    scene = _build_scene()
    first = _run(_start(scene))
    _run(finish_writeback(first.record_id, success=True, external_comment_id="CMT-1"))

    with pytest.raises(ConflictError) as caught:
        _run(_start(scene))

    assert caught.value.code == ErrorCode.WRITEBACK_ALREADY_SUCCESS
    assert len(_records(scene.task_id)) == 1, "重复提交不得产生第二条"


def test_a_changed_review_comment_produces_a_new_record() -> None:
    """复核内容变了 → 正文变了 → 新幂等键 → **允许**新的一条。"""
    scene = _build_scene()
    first = _run(_start(scene))
    _run(finish_writeback(first.record_id, success=True, external_comment_id="CMT-1"))

    _execute(
        "UPDATE risk_item SET review_status = 'MODIFIED', review_comment = %s WHERE id = %s",
        ("等级下调，已与业务确认", scene.risk_id),
    )
    second = _run(_start(scene))

    assert second.record_id != first.record_id
    assert second.idempotency_key != first.idempotency_key
    assert second.attempt == 1, "新内容是一条新记录，不是对旧记录的重试"
    assert "等级下调，已与业务确认" in second.content_md
    rows = _records(scene.task_id)
    assert len(rows) == 2
    assert [row[1] for row in rows] == ["success", "writing"]


def test_a_failed_attempt_is_retried_on_the_same_record() -> None:
    scene = _build_scene()
    first = _run(_start(scene))
    _run(finish_writeback(first.record_id, success=False, error_msg="审批系统 502"))

    retry = _run(_start(scene))

    assert retry.record_id == first.record_id, "同内容的重试复用同一条记录（幂等键相同）"
    assert retry.started is True
    assert retry.attempt == 2
    assert retry.status == WritebackStatus.WRITING.value
    row = _records(scene.task_id)[0]
    assert row[5] is None, "重试开始时清掉上一次的失败原因"
    assert len(_records(scene.task_id)) == 1


# --------------------------------------------------------------------------- #
# 5. 并发兜底（IntegrityError → 新事务重查）
# --------------------------------------------------------------------------- #
def test_a_concurrent_first_start_falls_back_to_the_existing_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个并发"首次发起"：输的那一方撞 UNIQUE，必须**重查后按既有记录返回**。

    这里用最小手段制造那一瞬间：先按 renderer 的同一套公式**预置**好"对方"那条记录，
    再把"按 key 预查"打桩成"查不到" —— 于是本次调用会一路走到 INSERT 并撞上
    ``UNIQUE(idempotency_key)``，正好落进生产代码的兜底分支。
    """
    scene = _build_scene()
    data = _run(load_report_data(scene.task_id))
    content = render_writeback(data.task, data.contract, data.risks)

    winner_id = _execute(
        "INSERT INTO writeback_record "
        "(task_id, contract_id, approval_instance_id, content_md, content_hash, status, attempt, "
        " idempotency_key, started_at, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, 'writing', 1, %s, NOW(3), NOW(3), NOW(3))",
        (
            scene.task_id,
            scene.contract_id,
            scene.instance_id,
            content.content_md,
            content.content_hash,
            content.idempotency_key,
        ),
    )

    import app.services.writeback as writeback_module

    async def _pretend_nothing_found(session, key):
        return None

    monkeypatch.setattr(writeback_module, "_load_by_key_for_update", _pretend_nothing_found)

    response = _run(_start(scene))

    monkeypatch.undo()

    assert response.record_id == winner_id, "应当返回对方那条记录，而不是报错"
    assert response.started is False
    assert len(_records(scene.task_id)) == 1, "并发不得产生第二条记录"


# --------------------------------------------------------------------------- #
# 6 / 7. 状态流转与持久化语义
# --------------------------------------------------------------------------- #
def test_finishing_with_success_persists_the_external_comment_id() -> None:
    scene = _build_scene()
    record = _run(_start(scene))

    result = _run(
        finish_writeback(
            record.record_id, success=True, external_comment_id="CMT-9", response_body='{"id":9}'
        )
    )

    assert result.status == WritebackStatus.SUCCESS.value
    assert result.external_comment_id == "CMT-9"
    assert result.error_msg is None
    assert result.finished_at is not None
    row = _records(scene.task_id)[0]
    assert row[1] == "success" and row[6] == "CMT-9" and row[5] is None
    assert (
        _scalar("SELECT response_body FROM writeback_record WHERE id = %s", (record.record_id,)) == '{"id":9}'
    )


def test_finishing_with_failure_persists_a_reason() -> None:
    scene = _build_scene()
    record = _run(_start(scene))

    result = _run(finish_writeback(record.record_id, success=False, error_msg="审批系统超时"))

    assert result.status == WritebackStatus.FAILED.value
    assert result.error_msg == "审批系统超时"
    assert result.external_comment_id is None
    assert result.finished_at is not None


def test_a_failure_without_a_reason_still_records_one() -> None:
    """一次失败却没有原因，排查时只能看到一句 ``failed``。"""
    scene = _build_scene()
    record = _run(_start(scene))

    result = _run(finish_writeback(record.record_id, success=False))

    assert result.error_msg, "失败原因必须**有值**"


def test_finishing_twice_is_refused() -> None:
    scene = _build_scene()
    record = _run(_start(scene))
    _run(finish_writeback(record.record_id, success=True, external_comment_id="CMT-1"))

    with pytest.raises(ConflictError) as caught:
        _run(finish_writeback(record.record_id, success=False, error_msg="再来一次"))

    assert caught.value.code == ErrorCode.INVALID_STATE_TRANSITION
    assert _records(scene.task_id)[0][1] == "success", "被拒的迁移不得改动既有状态"


def test_a_record_stuck_in_writing_is_refused_a_second_finish() -> None:
    """``writing`` 只能结束一次 —— 第二次说明调用方对状态的判断已经过期。"""
    scene = _build_scene()
    record = _run(_start(scene))
    _run(finish_writeback(record.record_id, success=False, error_msg="第一次就失败了"))

    with pytest.raises(ConflictError) as caught:
        _run(finish_writeback(record.record_id, success=False, error_msg="第二次"))

    assert caught.value.code == ErrorCode.INVALID_STATE_TRANSITION


def test_finishing_a_missing_record_is_a_not_found() -> None:
    with pytest.raises(NotFoundError) as caught:
        _run(finish_writeback(999_999_999, success=True))

    assert caught.value.code == ErrorCode.NOT_FOUND
