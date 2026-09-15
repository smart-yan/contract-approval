"""``GET /api/v1/contracts`` 的集成测试（**需要真实 MySQL**）。

这里断言的是**跨两张表的事实**：``latest_task`` 真的来自 ``review_task``
（而不是 ``contract`` 上那两个过期的冗余字段）、多任务时真的只取 id 最大的那条、
排序真的稳定。SQLite 上跑不出同样的语义。

隔离方式：自造 ``IT-CLIST-`` 前缀的合同，收尾按外键反序删除。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pymysql
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

PREFIX = "IT-CLIST-"
ENDPOINT = "/api/v1/contracts"


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


def _scalar(sql: str, params: tuple = ()):
    rows = _query(sql, params)
    return rows[0][0] if rows else None


def _purge() -> None:
    contract_ids = [
        row[0] for row in _query("SELECT id FROM contract WHERE contract_no LIKE %s", (f"{PREFIX}%",))
    ]
    if not contract_ids:
        return
    placeholders = ",".join(["%s"] * len(contract_ids))
    task_ids = [
        row[0]
        for row in _query(
            f"SELECT id FROM review_task WHERE contract_id IN ({placeholders})", tuple(contract_ids)
        )
    ]
    if task_ids:
        task_ph = ",".join(["%s"] * len(task_ids))
        for table in ("risk_item", "contract_metadata", "clause"):
            _query(f"DELETE FROM {table} WHERE task_id IN ({task_ph})", tuple(task_ids))
        _query(f"DELETE FROM review_task WHERE id IN ({task_ph})", tuple(task_ids))
    _query(f"DELETE FROM document_block WHERE contract_id IN ({placeholders})", tuple(contract_ids))
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


def _naive_utc(*args: int) -> datetime:
    """构造一个 **naive UTC** 时刻。

    与 ``app.utils.datetime_utils.utcnow`` 同一口径：项目统一把 UTC 存成
    ``DATETIME(3)``（**不带 tzinfo**）。这里刻意显式带上 ``UTC`` 再抹掉 tzinfo，
    而不是直接 ``datetime(...)`` —— 前者把"这是 UTC"写在脸上，
    也避开 ruff 的 DTZ001（裸构造会被当成"忘了时区"）。
    """
    return datetime(*args, tzinfo=UTC).replace(tzinfo=None)


def _insert_contract(
    *,
    created_at: datetime | None = None,
    status: str = "PENDING",
    current_task_id: int | None = None,
) -> int:
    """建一个合同。

    ``status`` / ``current_task_id`` 可以显式塞成**误导性**的值 —— 它们正是
    本接口**不该**采用的状态来源（P4 之后无人维护）。
    """
    _query(
        "INSERT INTO contract "
        "(contract_no, title, contract_type, source, status, current_task_id, created_at, updated_at) "
        "VALUES (%s, %s, 'PURCHASE', 'UPLOAD', %s, %s, %s, %s)",
        (
            f"{PREFIX}{uuid.uuid4().hex[:12]}",
            "列表测试合同",
            status,
            current_task_id,
            created_at or _naive_utc(2026, 1, 1, 0, 0, 0),
            created_at or _naive_utc(2026, 1, 1, 0, 0, 0),
        ),
    )
    return _scalar("SELECT MAX(id) FROM contract")


def _insert_file(contract_id: int) -> int:
    _query(
        "INSERT INTO contract_file "
        "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
        " created_at, updated_at) "
        "VALUES (%s, 'c.docx', '.docx', 1, %s, 'x', 0, 'PENDING', NOW(3), NOW(3))",
        (contract_id, uuid.uuid4().hex + uuid.uuid4().hex),
    )
    return _scalar("SELECT MAX(id) FROM contract_file")


def _insert_task(contract_id: int, file_id: int, *, status: str = "pending", stage: str = "UPLOADED") -> int:
    _query(
        "INSERT INTO review_task "
        "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
        " max_retry, idempotency_key, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 3, %s, NOW(3), NOW(3))",
        (contract_id, file_id, status, stage, uuid.uuid4().hex + uuid.uuid4().hex),
    )
    return _scalar("SELECT MAX(id) FROM review_task")


def _get(client: TestClient) -> list[dict]:
    """只取本模块造的合同 —— 库里可能还有别的测试残留，不假设自己是唯一。"""
    response = client.get(ENDPOINT)
    assert response.status_code == 200
    return [item for item in response.json() if item["contract_no"].startswith(PREFIX)]


# --------------------------------------------------------------------------- #
# 1：空数据
# --------------------------------------------------------------------------- #
def test_an_empty_result_is_two_hundred_with_an_empty_array(client: TestClient) -> None:
    """集合查询的空结果**不是 404** —— 它是一次成功的查询。"""
    response = client.get(ENDPOINT)

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert _get(client) == []


# --------------------------------------------------------------------------- #
# 2：没有任务的合同
# --------------------------------------------------------------------------- #
def test_a_contract_without_a_task_reports_a_null_latest_task(client: TestClient) -> None:
    """没有任务就是 ``null`` —— **不伪造一个默认任务**。"""
    contract_id = _insert_contract()

    (item,) = _get(client)

    assert item["contract_id"] == contract_id
    assert item["latest_task"] is None


# --------------------------------------------------------------------------- #
# 3：一个合同一个任务
# --------------------------------------------------------------------------- #
def test_a_single_task_is_returned_as_the_latest(client: TestClient) -> None:
    contract_id = _insert_contract()
    file_id = _insert_file(contract_id)
    task_id = _insert_task(contract_id, file_id, stage="CLAUSED")

    (item,) = _get(client)

    assert item["latest_task"] == {
        "task_id": task_id,
        "status": "pending",
        "current_stage": "CLAUSED",
        "progress": 60,
    }


# --------------------------------------------------------------------------- #
# 4：多个任务只取 id 最大的
# --------------------------------------------------------------------------- #
def test_only_the_newest_task_is_returned(client: TestClient) -> None:
    """一个合同下可以有多个任务（换规则集/换 prompt 就会新建），列表要的是**最近一次**。"""
    contract_id = _insert_contract()
    file_id = _insert_file(contract_id)
    _insert_task(contract_id, file_id, stage="UPLOADED")
    _insert_task(contract_id, file_id, stage="CLAUSED")
    newest = _insert_task(contract_id, file_id, stage="REVIEWED")

    (item,) = _get(client)

    assert item["latest_task"]["task_id"] == newest, "必须取 id 最大的那条"
    assert item["latest_task"]["current_stage"] == "REVIEWED"


def test_the_latest_task_is_chosen_per_contract(client: TestClient) -> None:
    """**多个合同各自算各自的** —— 别把 A 的最新任务串到 B 头上。"""
    first = _insert_contract(created_at=_naive_utc(2026, 1, 1))
    first_file = _insert_file(first)
    _insert_task(first, first_file, stage="UPLOADED")
    first_latest = _insert_task(first, first_file, stage="CLAUSED")

    second = _insert_contract(created_at=_naive_utc(2026, 1, 2))
    second_file = _insert_file(second)
    second_latest = _insert_task(second, second_file, stage="REVIEWED")

    items = {item["contract_id"]: item for item in _get(client)}

    assert items[first]["latest_task"]["task_id"] == first_latest
    assert items[second]["latest_task"]["task_id"] == second_latest


# --------------------------------------------------------------------------- #
# 5：排序稳定
# --------------------------------------------------------------------------- #
def test_contracts_are_ordered_by_created_at_then_id(client: TestClient) -> None:
    """``created_at DESC, id DESC`` —— 撞毫秒时靠 ``id`` 兜底才不会来回跳。"""
    old = _insert_contract(created_at=_naive_utc(2026, 1, 1))
    newest = _insert_contract(created_at=_naive_utc(2026, 3, 1))
    middle = _insert_contract(created_at=_naive_utc(2026, 2, 1))
    # 与 newest 同一时刻：应当排在它**后面**（id 更大 → 更晚插入）
    same_moment = _insert_contract(created_at=_naive_utc(2026, 3, 1))

    order = [item["contract_id"] for item in _get(client)]

    assert order == [same_moment, newest, middle, old]


def test_the_order_is_stable_across_requests(client: TestClient) -> None:
    """同样一批数据连查两次，次序必须一模一样（否则分页/对比都会失真）。"""
    for offset in range(4):
        _insert_contract(created_at=_naive_utc(2026, 5, 1) + timedelta(seconds=offset))
    _insert_contract(created_at=_naive_utc(2026, 5, 1))  # 制造一个撞时刻的

    assert _get(client) == _get(client)


# --------------------------------------------------------------------------- #
# 6：状态来自 ReviewTask，而不是 Contract 上那两个过期字段
# --------------------------------------------------------------------------- #
def test_the_status_comes_from_the_task_not_from_the_contract(client: TestClient) -> None:
    """**本接口最要紧的一条口径。**

    ``contract.status`` / ``contract.current_task_id`` 在 P4 之后无人维护。
    这里刻意把它们塞成**误导性**的值（``status='completed'``、``current_task_id``
    指向一条不存在的任务），而任务本身说的是 ``CLAUSED`` ——
    响应必须反映**任务**的真相。
    """
    contract_id = _insert_contract(status="completed", current_task_id=999_999)
    file_id = _insert_file(contract_id)
    task_id = _insert_task(contract_id, file_id, status="pending", stage="CLAUSED")

    (item,) = _get(client)

    assert item["latest_task"]["task_id"] == task_id
    assert item["latest_task"]["status"] == "pending", "取任务的状态，不是合同的"
    assert item["latest_task"]["current_stage"] == "CLAUSED"


def test_the_contract_does_not_expose_its_stale_status(client: TestClient) -> None:
    """响应里压根**没有** ``contract.status`` / ``current_task_id`` 这两个字段 ——
    不暴露就不会有人误用。"""
    _insert_contract(status="completed", current_task_id=999_999)

    (item,) = _get(client)

    assert set(item) == {"contract_id", "contract_no", "title", "contract_type", "created_at", "latest_task"}


# --------------------------------------------------------------------------- #
# 7：progress 与约定一致
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("stage", "expected"),
    [("UPLOADED", 0), ("PARSED", 30), ("CLAUSED", 60), ("REVIEWED", 100)],
)
def test_progress_follows_the_stage_mapping(client: TestClient, stage: str, expected: int) -> None:
    """进度由 ``current_stage`` 推导 —— 库里那一列 ``progress`` 恒为 0（无人维护）。"""
    contract_id = _insert_contract()
    file_id = _insert_file(contract_id)
    _insert_task(contract_id, file_id, stage=stage)

    (item,) = _get(client)

    assert item["latest_task"]["progress"] == expected
    assert (
        _scalar("SELECT progress FROM review_task WHERE id = %s", (item["latest_task"]["task_id"],)) == 0
    ), "数据库里那一列确实是 0 —— 进度是推导出来的"


# --------------------------------------------------------------------------- #
# 8：响应契约
# --------------------------------------------------------------------------- #
def test_the_response_matches_the_declared_schema(client: TestClient) -> None:
    contract_id = _insert_contract()
    file_id = _insert_file(contract_id)
    _insert_task(contract_id, file_id, stage="REVIEWED")

    (item,) = _get(client)

    assert isinstance(item["contract_id"], int)
    assert isinstance(item["contract_no"], str)
    assert isinstance(item["title"], str)
    assert item["contract_type"] == "PURCHASE"
    # ⚠️ naive UTC：项目统一存 UTC，**不带时区偏移**。前端要自行按本地时区换算。
    assert datetime.fromisoformat(item["created_at"]) == _naive_utc(2026, 1, 1)
    assert item["created_at"].endswith("T00:00:00"), "ISO 8601，且没有 Z/偏移后缀"
    assert isinstance(item["latest_task"]["progress"], int)


def test_the_response_is_not_an_orm_dump(client: TestClient) -> None:
    """响应是显式 DTO —— 不该把 ORM 的内部列（尤其是存储路径）漏出去。"""
    contract_id = _insert_contract()
    _insert_file(contract_id)

    (item,) = _get(client)

    body = str(item)
    assert "storage_path" not in body
    assert "our_party" not in item and "dept" not in item
