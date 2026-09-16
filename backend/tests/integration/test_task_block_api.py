"""``POST /api/v1/review-tasks/{task_id}/block`` 的集成测试（**需要真实 MySQL**）。

这里断言的是**只有真实数据库 + 真实 ASGI 才能验**的三件事：

1. **字段边界** —— 只写 ``status`` / ``block_reason_code`` / ``block_reason_msg``，
   其余列（尤其是 ``current_stage`` / ``finished_at`` / 业务结论）**逐列不变**
2. **状态机在服务端强制** —— 重复阻塞、阻塞终态任务都必须被拒，而不是静默覆盖
3. **404 与 409 的分工**

隔离方式：自造 ``TBLK-`` 前缀的合同 + ``TBLK-TASK-`` 前缀的幂等键，收尾按外键反序删除。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pymysql
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.base import UTC_SESSION_INIT_COMMAND
from app.main import app

PREFIX = "TBLK-"
TASK_KEY_PREFIX = "TBLK-TASK-"

#: 阻塞**不该**碰的列 —— 调用前后逐列比对
UNTOUCHED_COLUMNS = (
    "current_stage",
    "finished_at",
    "risk_level_final",
    "conclusion",
    "summary",
    "error_msg",
    "started_at",
    "retry_count",
    "worker_id",
    "locked_at",
    "heartbeat_at",
    "next_retry_at",
    "block_reason_code",  # 单独断言，不在这组"应保持不变"里
)
UNTOUCHED_COLUMNS = tuple(c for c in UNTOUCHED_COLUMNS if c != "block_reason_code")


def _connect() -> pymysql.connections.Connection:
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
        # 与应用的引擎同一条会话时区约定（见 app/db/base.build_connect_args）
        init_command=UTC_SESSION_INIT_COMMAND,
    )


def _db_available() -> tuple[bool, str]:
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


def _purge() -> None:
    task_ids = [
        row[0]
        for row in _query(
            "SELECT id FROM review_task WHERE idempotency_key LIKE %s", (f"{TASK_KEY_PREFIX}%",)
        )
    ]
    if task_ids:
        task_ph = ",".join(["%s"] * len(task_ids))
        _query(f"DELETE FROM review_task WHERE id IN ({task_ph})", tuple(task_ids))

    contract_ids = [
        row[0] for row in _query("SELECT id FROM contract WHERE contract_no LIKE %s", (f"{PREFIX}%",))
    ]
    if contract_ids:
        placeholders = ",".join(["%s"] * len(contract_ids))
        _query(f"DELETE FROM contract_file WHERE contract_id IN ({placeholders})", tuple(contract_ids))
        _query(f"DELETE FROM contract WHERE id IN ({placeholders})", tuple(contract_ids))


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    _purge()
    yield
    _purge()
    asyncio.run(_dispose())


async def _dispose() -> None:
    from app.db.session import dispose_engine

    await dispose_engine()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scenario:
    contract_id: int
    file_id: int
    task_id: int


def _build_scenario(*, status: str = "pending", stage: str = "CLAUSED") -> Scenario:
    contract_id = _execute(
        "INSERT INTO contract "
        "(contract_no, title, contract_type, source, status, created_at, updated_at) "
        "VALUES (%s, '阻塞测试合同', 'PURCHASE', 'UPLOAD', 'PENDING', NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:10]}",),
    )
    file_id = _execute(
        "INSERT INTO contract_file "
        "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
        " created_at, updated_at) "
        "VALUES (%s, 'contract.docx', '.docx', 1024, %s, 'x', 0, 'PARSED', NOW(3), NOW(3))",
        (contract_id, uuid.uuid4().hex + uuid.uuid4().hex),
    )
    task_id = _execute(
        "INSERT INTO review_task "
        "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
        " max_retry, idempotency_key, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 3, %s, NOW(3), NOW(3))",
        (contract_id, file_id, status, stage, f"{TASK_KEY_PREFIX}{uuid.uuid4().hex}"),
    )
    return Scenario(contract_id=contract_id, file_id=file_id, task_id=task_id)


def _row(task_id: int) -> dict:
    columns = (
        "status",
        "block_reason_code",
        "block_reason_msg",
        *UNTOUCHED_COLUMNS,
    )
    values = _query(f"SELECT {', '.join(columns)} FROM review_task WHERE id = %s", (task_id,))[0]
    return dict(zip(columns, values, strict=True))


def _block(client: TestClient, task_id: int, **body):
    payload = {"block_reason_code": "AGENT_GRAPH_EXECUTION_FAILED", "block_reason_msg": "图执行抛异常"}
    payload.update(body)
    return client.post(f"/api/v1/review-tasks/{task_id}/block", json=payload)


# --------------------------------------------------------------------------- #
# 1：成功路径
# --------------------------------------------------------------------------- #
def test_a_pending_task_can_be_blocked(client: TestClient) -> None:
    scene = _build_scenario()

    response = _block(client, scene.task_id)

    assert response.status_code == 200, response.text
    row = _row(scene.task_id)
    assert row["status"] == "blocked"
    assert row["block_reason_code"] == "AGENT_GRAPH_EXECUTION_FAILED"
    assert row["block_reason_msg"] == "图执行抛异常"


def test_the_response_echoes_the_persisted_values(client: TestClient) -> None:
    scene = _build_scenario()

    body = _block(client, scene.task_id, block_reason_msg="后台执行抛出 ValueError").json()

    assert body["task_id"] == scene.task_id
    assert body["status"] == "blocked"
    assert body["block_reason_msg"] == "后台执行抛出 ValueError"
    assert _row(scene.task_id)["block_reason_msg"] == body["block_reason_msg"]


@pytest.mark.parametrize("stage", ["UPLOADED", "PARSED", "CLAUSED", "REVIEWED"])
def test_blocking_works_at_every_stage(client: TestClient, stage: str) -> None:
    """**阶段不影响能否阻塞** —— 跑挂了就是跑挂了，与跑到哪一步无关。"""
    scene = _build_scenario(stage=stage)

    assert _block(client, scene.task_id).status_code == 200
    assert _row(scene.task_id)["current_stage"] == stage, "阶段不该被本接口改动"


# --------------------------------------------------------------------------- #
# 2：字段边界 —— 只写三个字段
# --------------------------------------------------------------------------- #
def test_blocking_touches_nothing_but_the_three_fields(client: TestClient) -> None:
    """``current_stage`` / ``finished_at`` / 业务结论 / 重试与抢占列 **逐列不变**。

    ⚠️ 尤其是 ``current_stage``：它表达"已完成到哪一步"（断点续跑依据）。
    任务失败**不代表阶段回退或前进** —— 一份已切分完条款的输入跑挂了，
    它的阶段仍然诚实地是 ``CLAUSED``。
    """
    scene = _build_scenario()
    before = _row(scene.task_id)

    assert _block(client, scene.task_id).status_code == 200

    after = _row(scene.task_id)
    for column in UNTOUCHED_COLUMNS:
        assert after[column] == before[column], f"{column} 被阻塞接口改动了"
    assert after["status"] != before["status"], "status 本来就该变"


def test_the_stage_is_not_rolled_back_or_advanced(client: TestClient) -> None:
    scene = _build_scenario(stage="CLAUSED")

    _block(client, scene.task_id)

    assert _row(scene.task_id)["current_stage"] == "CLAUSED"


def test_finished_at_stays_null(client: TestClient) -> None:
    """阻塞 ≠ 结束。填了 ``finished_at`` 就会与 ``status=blocked`` 自相矛盾。"""
    scene = _build_scenario()

    _block(client, scene.task_id)

    assert _row(scene.task_id)["finished_at"] is None


# --------------------------------------------------------------------------- #
# 3：状态机在服务端强制
# --------------------------------------------------------------------------- #
def test_blocking_twice_is_refused(client: TestClient) -> None:
    """重复阻塞 → 409。

    第二次调用通常意味着调用方对状态的判断已经过期（例如两个后台任务都以为
    自己失败了）。静默成功会把"有人重复处理"这件事藏起来。
    """
    scene = _build_scenario()
    assert _block(client, scene.task_id).status_code == 200

    response = _block(client, scene.task_id, block_reason_msg="第二次")

    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_STATE_TRANSITION"
    # 而且第一次的原因**没有被覆盖**
    assert _row(scene.task_id)["block_reason_msg"] == "图执行抛异常"


def test_a_completed_task_can_not_be_blocked(client: TestClient) -> None:
    """``completed`` 是终态（矩阵里是空集）。"""
    scene = _build_scenario(status="completed", stage="REVIEWED")

    response = _block(client, scene.task_id)

    assert response.status_code == 409
    assert _row(scene.task_id)["status"] == "completed"


def test_an_unknown_status_can_not_be_blocked(client: TestClient) -> None:
    """不认得的当前状态一律拒绝 —— 白名单，不是黑名单。"""
    scene = _build_scenario(status="SOMETHING_NEW")

    assert _block(client, scene.task_id).status_code == 409


# --------------------------------------------------------------------------- #
# 4：404 / 422
# --------------------------------------------------------------------------- #
def test_a_missing_task_is_a_404(client: TestClient) -> None:
    response = _block(client, 99999999)

    assert response.status_code == 404
    assert response.json()["code"] == "TASK_NOT_FOUND"


def test_an_unregistered_reason_code_is_refused(client: TestClient) -> None:
    """该列没有 CHECK 约束，校验只能在应用层做（§6.1 枚举化的三条理由都依赖它）。"""
    scene = _build_scenario()

    response = _block(client, scene.task_id, block_reason_code="MADE_UP_CODE")

    assert response.status_code == 422
    assert _row(scene.task_id)["status"] == "pending", "校验失败不得留下任何改动"


@pytest.mark.parametrize("missing", ["block_reason_code", "block_reason_msg"])
def test_both_reason_fields_are_required(client: TestClient, missing: str) -> None:
    scene = _build_scenario()
    payload = {"block_reason_code": "AGENT_GRAPH_EXECUTION_FAILED", "block_reason_msg": "x"}
    payload.pop(missing)

    response = client.post(f"/api/v1/review-tasks/{scene.task_id}/block", json=payload)

    assert response.status_code == 422
    assert _row(scene.task_id)["status"] == "pending"


def test_the_reason_message_respects_the_column_width(client: TestClient) -> None:
    """``block_reason_msg`` 是 ``String(512)`` —— 超长必须在**进入数据库之前**被拦下。"""
    scene = _build_scenario()

    assert _block(client, scene.task_id, block_reason_msg="字" * 513).status_code == 422
    assert _block(client, scene.task_id, block_reason_msg="字" * 512).status_code == 200


def test_the_code_respects_the_column_width(client: TestClient) -> None:
    scene = _build_scenario()

    assert _block(client, scene.task_id, block_reason_code="X" * 33).status_code == 422


# --------------------------------------------------------------------------- #
# 5：只影响目标任务
# --------------------------------------------------------------------------- #
def test_blocking_one_task_does_not_touch_another(client: TestClient) -> None:
    first = _build_scenario()
    second = _build_scenario()

    assert _block(client, first.task_id).status_code == 200

    assert _row(first.task_id)["status"] == "blocked"
    assert _row(second.task_id)["status"] == "pending"
