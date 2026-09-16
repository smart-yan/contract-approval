"""回写闭环的集成测试（**需要真实 MySQL**，P15-3b）。

覆盖"**本地状态机** + **外部审批系统**"合起来的行为，其中几件在 mock 里根本测不出来：

* **幂等**：同一条意见重复提交，外部只留一条评论（本地 UNIQUE + 外部按幂等键去重，
  两层都真的成立）
* **"外部写成功但响应丢了"**（§12 第 5 步；§14.3 的 ``duplicate_comment`` 就是演示它）：
  本地停在 ``failed``/``writing``，可外部的评论**其实已经存在** —— 恢复必须先
  ``find``，且必须用**同一个**幂等键
* **审批单从哪来**：只认 ``ReviewTask → Contract → contract.approval_instance_id``；
  没关联就是 ``WRITEBACK_APPROVAL_NOT_READY``（**不猜、不取第一条**）

隔离方式：自造 ``IT-WB3-`` 前缀的合同与审批单，收尾按外键反序删除。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pymysql
import pytest
from fastapi.testclient import TestClient

from app.core.constants import WritebackStatus
from app.core.errors import ConflictError, ErrorCode, NotFoundError
from app.integrations.approval import ApprovalClient, ApprovalCommentRef, ApprovalSystemError
from app.integrations.mock_approval import MockApprovalClient
from app.main import app
from app.services.writeback import start_writeback
from app.services.writeback_execution import execute_writeback, resolve_approval_instance_id

PREFIX = "IT-WB3-"


# --------------------------------------------------------------------------- #
# 数据库小工具（与其它集成测试同一套写法）
# --------------------------------------------------------------------------- #
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


def _purge() -> None:
    instance_ids = [
        row[0]
        for row in _query("SELECT id FROM approval_instance WHERE instance_no LIKE %s", (f"{PREFIX}%",))
    ]
    if instance_ids:
        ph = ",".join(["%s"] * len(instance_ids))
        _query(f"DELETE FROM approval_comment WHERE instance_id IN ({ph})", tuple(instance_ids))

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
            _query(f"DELETE FROM risk_item WHERE task_id IN ({task_ph})", tuple(task_ids))
            _query(f"DELETE FROM review_task WHERE id IN ({task_ph})", tuple(task_ids))
        _query(f"DELETE FROM contract_file WHERE contract_id IN ({ph})", tuple(contract_ids))
        _query(f"DELETE FROM contract WHERE id IN ({ph})", tuple(contract_ids))

    if instance_ids:
        ph = ",".join(["%s"] * len(instance_ids))
        _query(f"DELETE FROM approval_instance WHERE id IN ({ph})", tuple(instance_ids))


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    _purge()
    yield
    _purge()


async def _dispose() -> None:
    from app.db.session import dispose_engine

    await dispose_engine()


def _run(coro):
    """在同步测试里跑协程（每个用例自建事件循环，跑完释放引擎）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(_dispose())
        loop.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scene:
    contract_id: int
    task_id: int
    instance_id: int


def _insert_instance(instance_no: str | None = None) -> int:
    return _execute(
        "INSERT INTO approval_instance "
        "(instance_no, title, applicant, dept, amount, status, contract_id, created_at, updated_at) "
        "VALUES (%s, '回写闭环测试审批单', '张三', '法务部', 1000.00, 'PENDING', NULL, NOW(3), NOW(3))",
        (instance_no or f"{PREFIX}{uuid.uuid4().hex[:12]}",),
    )


def _insert_contract(*, bound_instance_id: int | None) -> int:
    return _execute(
        "INSERT INTO contract "
        "(contract_no, title, contract_type, source, status, approval_instance_id, created_at, updated_at) "
        "VALUES (%s, '回写闭环测试合同', 'PURCHASE', 'UPLOAD', 'PENDING', %s, NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:12]}", bound_instance_id),
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


def _insert_risk(task_id: int, contract_id: int, *, title: str = "知识产权归属相对方") -> int:
    return _execute(
        "INSERT INTO risk_item "
        "(task_id, contract_id, clause_id, risk_code, risk_title, dimension, risk_level, source, "
        " reason, legal_basis, original_text, paragraph_index, locator_type, review_status, "
        " created_at, updated_at) "
        "VALUES (%s, %s, NULL, 'IP_OWNER_SUPPLIER_001', %s, '知识产权', 'HIGH', 'RULE', '成因', '依据', "
        "        '命中片段', 23, 'PARAGRAPH', 'PENDING', NOW(3), NOW(3))",
        (task_id, contract_id, title),
    )


def _build_scene(*, stage: str = "REVIEWED", bind: bool = True, with_risk: bool = True) -> Scene:
    instance_id = _insert_instance()
    contract_id = _insert_contract(bound_instance_id=instance_id if bind else None)
    file_id = _insert_file(contract_id)
    task_id = _insert_task(contract_id, file_id, stage=stage)
    if with_risk:
        _insert_risk(task_id, contract_id)
    return Scene(contract_id=contract_id, task_id=task_id, instance_id=instance_id)


def _comments(instance_id: int) -> list[tuple]:
    return _query(
        "SELECT id, external_id, idempotency_key, source, comment_type, author, content "
        "FROM approval_comment WHERE instance_id = %s ORDER BY id",
        (instance_id,),
    )


def _record(task_id: int) -> tuple:
    return _query(
        "SELECT id, status, attempt, external_comment_id, error_msg, idempotency_key "
        "FROM writeback_record WHERE task_id = %s ORDER BY id",
        (task_id,),
    )[0]


class _CountingClient(ApprovalClient):
    """真客户端外面套一层计数器 —— 用来断言"**该不该发**写请求"。"""

    def __init__(self, inner: ApprovalClient) -> None:
        self._inner = inner
        self.finds = 0
        self.posts = 0

    async def find_comment(self, instance_id: int, idempotency_key: str) -> ApprovalCommentRef | None:
        self.finds += 1
        return await self._inner.find_comment(instance_id, idempotency_key)

    async def post_comment(
        self, instance_id: int, content_md: str, idempotency_key: str
    ) -> ApprovalCommentRef:
        self.posts += 1
        return await self._inner.post_comment(instance_id, content_md, idempotency_key)


class _LostResponseClient(ApprovalClient):
    """**外部真的写成功了，但响应丢了**。

    第 1 次 ``post_comment``：先把评论写进 Mock（真实落库），然后抛超时 ——
    调用方看到的是失败，而外部其实已经有了这条评论（§14.3 的 ``duplicate_comment``）。
    之后的调用恢复正常。
    """

    def __init__(self, inner: ApprovalClient) -> None:
        self._inner = inner
        self.lost_once = False
        self.posts = 0

    async def find_comment(self, instance_id: int, idempotency_key: str) -> ApprovalCommentRef | None:
        return await self._inner.find_comment(instance_id, idempotency_key)

    async def post_comment(
        self, instance_id: int, content_md: str, idempotency_key: str
    ) -> ApprovalCommentRef:
        self.posts += 1
        ref = await self._inner.post_comment(instance_id, content_md, idempotency_key)
        if not self.lost_once:
            self.lost_once = True
            raise ApprovalSystemError("审批系统超时：响应丢失")
        return ref


# --------------------------------------------------------------------------- #
# 1 / 3：正常首次回写
# --------------------------------------------------------------------------- #
def test_a_first_writeback_reaches_success_and_stores_the_external_id() -> None:
    scene = _build_scene()
    mock = MockApprovalClient()

    result = _run(execute_writeback(scene.task_id, client=mock))

    assert result.status == WritebackStatus.SUCCESS.value
    assert result.posted is True
    assert result.approval_instance_id == scene.instance_id
    assert result.error_msg is None and result.finished_at is not None

    comments = _comments(scene.instance_id)
    assert len(comments) == 1
    comment_id, external_id, key, source, comment_type, author, content = comments[0]
    assert source == "CONTRACT_REVIEW_SYSTEM", "§7.2 规定的回写来源"
    assert comment_type == "SYSTEM_REVIEW"
    assert author, "评论人必填（系统评论用一个固定标识，不伪造人名）"
    assert external_id == result.external_comment_id == f"MOCK-CMT-{comment_id}"
    assert "知识产权归属相对方" in content, "评论正文就是 P15-1 渲染出来的那份快照"

    record = _record(scene.task_id)
    assert record[1] == "success" and record[3] == external_id and record[5] == key


def test_the_comment_body_is_the_renderer_snapshot() -> None:
    """外部系统收到的那段文字与本地 ``writeback_record.content_md`` 逐字一致。"""
    scene = _build_scene()

    _run(execute_writeback(scene.task_id, client=MockApprovalClient()))

    stored = _scalar("SELECT content_md FROM writeback_record WHERE task_id = %s", (scene.task_id,))
    assert _comments(scene.instance_id)[0][6] == stored


# --------------------------------------------------------------------------- #
# 2 / 9：幂等
# --------------------------------------------------------------------------- #
def test_a_repeat_writeback_does_not_create_a_second_external_comment() -> None:
    scene = _build_scene()
    first = _run(execute_writeback(scene.task_id, client=MockApprovalClient()))

    with pytest.raises(ConflictError) as caught:
        _run(execute_writeback(scene.task_id, client=MockApprovalClient()))

    assert caught.value.code == ErrorCode.WRITEBACK_ALREADY_SUCCESS
    assert len(_comments(scene.instance_id)) == 1, "重复提交不得产生第二条评论"
    assert _record(scene.task_id)[3] == first.external_comment_id


def test_the_mock_returns_the_existing_comment_for_the_same_key() -> None:
    """§14.2：**重复键返回既有评论不新建**（Mock 侧直接验证）。"""
    scene = _build_scene()
    mock = MockApprovalClient()

    first = _run(mock.post_comment(scene.instance_id, "正文", "k" * 64))
    second = _run(mock.post_comment(scene.instance_id, "正文", "k" * 64))

    assert first.external_id == second.external_id
    assert len(_comments(scene.instance_id)) == 1


def test_the_same_key_on_two_instances_creates_two_comments() -> None:
    """幂等键是"外部**请求**的身份"，两个审批单互不干扰（P15-3a 的约束语义）。"""
    scene = _build_scene()
    other_instance = _insert_instance()
    mock = MockApprovalClient()

    first = _run(mock.post_comment(scene.instance_id, "正文", "k" * 64))
    second = _run(mock.post_comment(other_instance, "正文", "k" * 64))

    assert first.external_id != second.external_id
    assert len(_comments(scene.instance_id)) == 1
    assert len(_comments(other_instance)) == 1


# --------------------------------------------------------------------------- #
# 4：外部已有 → 不 post
# --------------------------------------------------------------------------- #
def test_an_existing_external_comment_is_reused_without_posting() -> None:
    """外部已经写过这条意见、本地却没记录（记录丢了）→ **只 find、不 post**。

    这条与"响应丢失"是同一枚硬币的两面：**能回答"外部到底写了没有"的只有外部系统**。
    本地记录在不在、是 success 还是 writing，都不足以判断。
    """
    scene = _build_scene()
    mock = MockApprovalClient()

    first = _run(execute_writeback(scene.task_id, client=mock))
    assert first.status == WritebackStatus.SUCCESS.value
    # 本地记录丢了（换库、被清理、回滚……），外部那条评论还在
    _execute("DELETE FROM writeback_record WHERE task_id = %s", (scene.task_id,))

    counting = _CountingClient(mock)
    result = _run(execute_writeback(scene.task_id, client=counting))

    assert counting.finds == 1
    assert counting.posts == 0, "外部已经有了，不该再发一次"
    assert result.status == WritebackStatus.SUCCESS.value
    assert result.posted is False
    assert result.external_comment_id == first.external_comment_id
    assert len(_comments(scene.instance_id)) == 1


# --------------------------------------------------------------------------- #
# 5 / 6：外部失败，以及"外部成功但响应丢了"
# --------------------------------------------------------------------------- #
def test_a_transport_failure_lands_in_failed_with_a_reason() -> None:
    scene = _build_scene()

    class _BrokenClient(ApprovalClient):
        async def find_comment(self, instance_id: int, idempotency_key: str):
            return None

        async def post_comment(self, instance_id: int, content_md: str, idempotency_key: str):
            raise ApprovalSystemError("审批系统不可达")

    result = _run(execute_writeback(scene.task_id, client=_BrokenClient()))

    assert result.status == WritebackStatus.FAILED.value
    assert result.error_msg == "审批系统不可达"
    assert result.external_comment_id is None
    assert _comments(scene.instance_id) == [], "写失败时外部不该留下任何东西"
    assert _record(scene.task_id)[1] == "failed"


def test_a_lost_response_is_recovered_without_a_second_comment() -> None:
    """**本文件最重要的一条**：外部写成功、响应丢了 → 本地 failed，但评论已经在外部。

    恢复时先 ``find``，找到就认回来（``success`` + 不 post），
    全程只有**一条**评论、用的还是**同一个**幂等键。
    """
    scene = _build_scene()
    lost = _LostResponseClient(MockApprovalClient())

    first = _run(execute_writeback(scene.task_id, client=lost))

    assert first.status == WritebackStatus.FAILED.value, "响应丢了，本地只能如实记失败"
    assert "超时" in (first.error_msg or "")
    assert len(_comments(scene.instance_id)) == 1, "但外部其实已经写进去了"
    key = _record(scene.task_id)[5]

    # ---- 调用方显式重试（没有 worker / 定时任务，就是再调一次）----
    second = _run(execute_writeback(scene.task_id, client=lost))

    assert second.status == WritebackStatus.SUCCESS.value
    assert second.posted is False, "find 命中 → 没有再发一次"
    assert second.external_comment_id == _comments(scene.instance_id)[0][1]
    assert len(_comments(scene.instance_id)) == 1, "绝不产生第二条评论"
    assert _record(scene.task_id)[5] == key, "幂等键自始至终没有变过"


# --------------------------------------------------------------------------- #
# 7 / 8：WRITING 的恢复
# --------------------------------------------------------------------------- #
def test_reconcile_finds_the_comment_and_finishes_success() -> None:
    """记录停在 ``writing``、外部却已有评论（上一次写完就断了）→ find 认回来。"""
    scene = _build_scene()
    mock = MockApprovalClient()
    attempt = _run(start_writeback(scene.task_id, scene.instance_id))
    assert _record(scene.task_id)[1] == "writing"

    # 外部那条其实已经写进去了
    ref = _run(mock.post_comment(scene.instance_id, attempt.content_md, attempt.idempotency_key))

    counting = _CountingClient(mock)
    result = _run(execute_writeback(scene.task_id, client=counting))

    assert result.record_id == attempt.record_id, "复用同一条记录，不新建幂等身份"
    assert result.status == WritebackStatus.SUCCESS.value
    assert result.external_comment_id == ref.external_id
    assert counting.posts == 0
    assert len(_comments(scene.instance_id)) == 1


def test_reconcile_posts_with_the_original_key_when_nothing_is_found() -> None:
    """记录停在 ``writing``、外部**确实没有** → 用**原来那把**键补写。"""
    scene = _build_scene()
    mock = MockApprovalClient()
    attempt = _run(start_writeback(scene.task_id, scene.instance_id))
    original_key = attempt.idempotency_key

    counting = _CountingClient(mock)
    result = _run(execute_writeback(scene.task_id, client=counting))

    assert counting.posts == 1, "外部没有就该补写"
    assert result.status == WritebackStatus.SUCCESS.value
    assert result.record_id == attempt.record_id
    comments = _comments(scene.instance_id)
    assert len(comments) == 1
    assert comments[0][2] == original_key, "用的必须是**原来那把**键，绝不重新生成"


# --------------------------------------------------------------------------- #
# 11 / 12 / 13 / 14：门禁与解析
# --------------------------------------------------------------------------- #
def test_a_contract_without_an_approval_instance_is_refused() -> None:
    """合同没关联审批单 → ``WRITEBACK_APPROVAL_NOT_READY``，且**不猜**任何审批单。"""
    scene = _build_scene(bind=False)
    other_instance = _insert_instance()  # 库里存在别的审批单，也不许顺手拿来用

    with pytest.raises(ConflictError) as caught:
        _run(resolve_approval_instance_id(scene.task_id))

    assert caught.value.code == ErrorCode.WRITEBACK_APPROVAL_NOT_READY
    assert caught.value.http_status == 409

    with pytest.raises(ConflictError):
        _run(execute_writeback(scene.task_id, client=MockApprovalClient()))

    assert _comments(other_instance) == []
    assert _query("SELECT id FROM writeback_record WHERE task_id = %s", (scene.task_id,)) == []


def test_a_dangling_approval_instance_is_reported_clearly() -> None:
    """合同指向一行不存在的审批单（该列没有 FK）→ 明确报错，而不是 500。"""
    scene = _build_scene(bind=False)
    _execute("UPDATE contract SET approval_instance_id = %s WHERE id = %s", (999_999_999, scene.contract_id))

    with pytest.raises(NotFoundError) as caught:
        _run(resolve_approval_instance_id(scene.task_id))

    assert caught.value.code == ErrorCode.NOT_FOUND
    assert caught.value.details == {
        "contract_id": scene.contract_id,
        "approval_instance_id": 999_999_999,
    }


def test_a_missing_task_is_a_task_not_found() -> None:
    with pytest.raises(NotFoundError) as caught:
        _run(resolve_approval_instance_id(999_999_999))

    assert caught.value.code == ErrorCode.TASK_NOT_FOUND


def test_a_task_that_is_not_reviewed_is_still_refused_by_the_p15_2_gate() -> None:
    """P15-2 的门禁语义没被绕过：阶段没到 REVIEWED 就是 ``WRITEBACK_NOT_READY``。"""
    scene = _build_scene(stage="CLAUSED")

    with pytest.raises(ConflictError) as caught:
        _run(execute_writeback(scene.task_id, client=MockApprovalClient()))

    assert caught.value.code == ErrorCode.WRITEBACK_NOT_READY
    assert _comments(scene.instance_id) == []


def test_an_external_business_rejection_lands_in_failed_and_propagates() -> None:
    """外部**明确拒绝**（业务错误）时：本地记 ``failed``，错误原样给调用方。

    不能因为"调用方会看到异常"就让本地停在 ``writing`` —— 那会留下一条
    "永远在写"的记录，谁也不知道它其实早就被拒了。
    """
    scene = _build_scene()

    class _RejectingClient(ApprovalClient):
        async def find_comment(self, instance_id: int, idempotency_key: str):
            return None

        async def post_comment(self, instance_id: int, content_md: str, idempotency_key: str):
            raise NotFoundError(
                f"审批单 {instance_id} 不存在，无法写入评论",
                code=ErrorCode.NOT_FOUND,
                details={"approval_instance_id": instance_id},
            )

    with pytest.raises(NotFoundError) as caught:
        _run(execute_writeback(scene.task_id, client=_RejectingClient()))

    assert caught.value.code == ErrorCode.NOT_FOUND
    assert _record(scene.task_id)[1] == "failed"
    assert _record(scene.task_id)[4], "失败原因要留在本地记录上"
    assert _comments(scene.instance_id) == []


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_the_api_writes_back_and_returns_the_run_result(client: TestClient) -> None:
    scene = _build_scene()

    response = client.post(f"/api/v1/review-tasks/{scene.task_id}/writeback")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert body["posted"] is True
    assert body["approval_instance_id"] == scene.instance_id
    assert body["external_comment_id"]
    assert len(_comments(scene.instance_id)) == 1


def test_the_api_refuses_a_contract_without_an_approval_instance(client: TestClient) -> None:
    scene = _build_scene(bind=False)

    response = client.post(f"/api/v1/review-tasks/{scene.task_id}/writeback")

    assert response.status_code == 409
    assert response.json()["code"] == "WRITEBACK_APPROVAL_NOT_READY"


def test_the_api_reports_a_missing_task_as_404(client: TestClient) -> None:
    response = client.post("/api/v1/review-tasks/99999999/writeback")

    assert response.status_code == 404
    assert response.json()["code"] == "TASK_NOT_FOUND"


def test_the_api_is_idempotent_on_repeat(client: TestClient) -> None:
    scene = _build_scene()

    first = client.post(f"/api/v1/review-tasks/{scene.task_id}/writeback")
    second = client.post(f"/api/v1/review-tasks/{scene.task_id}/writeback")

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "WRITEBACK_ALREADY_SUCCESS"
    assert len(_comments(scene.instance_id)) == 1


def test_the_api_does_not_accept_an_approval_instance_id(client: TestClient) -> None:
    """调用方**不能**指定审批单 —— 它由后端从合同解析（多传的字段被忽略）。"""
    scene = _build_scene()
    other_instance = _insert_instance()

    response = client.post(
        f"/api/v1/review-tasks/{scene.task_id}/writeback",
        json={"approval_instance_id": other_instance},
    )

    assert response.status_code == 200
    assert response.json()["approval_instance_id"] == scene.instance_id
    assert _comments(other_instance) == []
