"""``POST /api/v1/review-tasks/{task_id}/document`` 的集成测试（**需要真实 MySQL**）。

为什么放在 integration
---------------------
这里断言的全是**数据库层面**的事实：外键真的指向本次写入的块、文件级复用真的
没有重复插入、失败真的整体回滚、并发真的只有一个赢家。SQLite 上跑不出同样的语义。

隔离方式
--------
本模块自己造 ``contract`` / ``contract_file`` / ``review_task``（``IT-DOC-`` 前缀），
收尾按外键依赖的反序删除。租户数据只读不写。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pymysql
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.main import app

PREFIX = "IT-DOC-"
CONTRACT_TYPE = "PURCHASE"


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
    else:
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
        _query(f"DELETE FROM risk_item WHERE task_id IN ({task_ph})", tuple(task_ids))
        _query(f"DELETE FROM contract_metadata WHERE task_id IN ({task_ph})", tuple(task_ids))
        _query(f"DELETE FROM clause WHERE task_id IN ({task_ph})", tuple(task_ids))
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
def _insert_contract() -> int:
    _query(
        "INSERT INTO contract (contract_no, title, contract_type, source, status, created_at, updated_at) "
        "VALUES (%s, %s, %s, 'UPLOAD', 'PENDING', NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:12]}", "文档层测试合同", CONTRACT_TYPE),
    )
    return _scalar("SELECT MAX(id) FROM contract")


def _insert_file(contract_id: int, *, file_ext: str = "docx") -> int:
    _query(
        "INSERT INTO contract_file "
        "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
        " created_at, updated_at) "
        "VALUES (%s, %s, %s, 1024, %s, %s, 0, 'PENDING', NOW(3), NOW(3))",
        (
            contract_id,
            f"contract.{file_ext}",
            file_ext,
            uuid.uuid4().hex + uuid.uuid4().hex,
            f"{contract_id}/contract.{file_ext}",
        ),
    )
    return _scalar("SELECT MAX(id) FROM contract_file")


def _insert_task(contract_id: int, file_id: int) -> int:
    _query(
        "INSERT INTO review_task "
        "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
        " max_retry, idempotency_key, created_at, updated_at) "
        "VALUES (%s, %s, 'pending', 'UPLOADED', 0, 0, 0, 0, 3, %s, NOW(3), NOW(3))",
        (contract_id, file_id, uuid.uuid4().hex + uuid.uuid4().hex),
    )
    return _scalar("SELECT MAX(id) FROM review_task")


@pytest.fixture
def fixture_ids() -> Iterator[dict[str, int]]:
    contract_id = _insert_contract()
    file_id = _insert_file(contract_id)
    task_id = _insert_task(contract_id, file_id)
    yield {"contract_id": contract_id, "file_id": file_id, "task_id": task_id}


# --------------------------------------------------------------------------- #
# 请求构造
# --------------------------------------------------------------------------- #
_TEXTS = ("第一条 知识产权", "本项目产生的知识产权归乙方所有。", "第二条 违约责任")


def _blocks() -> list[dict[str, Any]]:
    """三段文本，全局偏移按 `ParseResult.text == "\\n".join(paragraphs)` 累积。"""
    blocks: list[dict[str, Any]] = []
    cursor = 0
    for index, text in enumerate(_TEXTS):
        blocks.append(
            {
                "order_index": index,
                "paragraph_index": index,
                "block_type": "PARAGRAPH",
                "text": text,
                "char_start_global": cursor,
                "char_end_global": cursor + len(text),
            }
        )
        cursor += len(text) + 1
    return blocks


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "parse_status": "PARSED",
        "blocks": _blocks(),
        "clauses": [
            {
                "clause_type": "IP",
                "text": "\n".join(_TEXTS[:2]),
                "extract_method": "RULE",
                "clause_no": "第一条",
                "title": "知识产权",
                "start_block_index": 0,
                "end_block_index": 1,
            },
            {
                "clause_type": "LIABILITY",
                "text": _TEXTS[2],
                "extract_method": "RULE",
                "start_block_index": 2,
                "end_block_index": 2,
            },
        ],
        "metadata": [
            {
                "field_key": "counterparty_name",
                "field_label": "相对方名称",
                "field_value": "乙方",
                "value_type": "TEXT",
                "extract_method": "REGEX",
                "source_block_index": 1,
            }
        ],
    }
    payload.update(overrides)
    return payload


def _post(client: TestClient, task_id: int, payload: dict[str, Any]):
    return client.post(f"/api/v1/review-tasks/{task_id}/document", json=payload)


def _blocks_of(contract_id: int) -> list[tuple]:
    return _query(
        "SELECT order_index, paragraph_index, block_type, text, raw_text, "
        "       char_start_in_block, char_end_in_block, char_start_global, char_end_global, locator_type "
        "FROM document_block WHERE contract_id = %s ORDER BY order_index",
        (contract_id,),
    )


# --------------------------------------------------------------------------- #
# 1 / 2：正常写入与 FK
# --------------------------------------------------------------------------- #
def test_blocks_clauses_and_metadata_are_all_written(client: TestClient, fixture_ids: dict[str, int]) -> None:
    response = _post(client, fixture_ids["task_id"], _payload())

    assert response.status_code == 201
    body = response.json()
    assert body["blocks_created"] == 3
    assert body["blocks_reused"] == 0
    assert body["clauses_persisted"] == 2
    assert body["metadata_persisted"] == 1
    assert body["parse_status"] == "PARSED"
    assert body["current_stage"] == "CLAUSED"

    assert len(_blocks_of(fixture_ids["contract_id"])) == 3
    assert _scalar("SELECT COUNT(*) FROM clause WHERE task_id = %s", (fixture_ids["task_id"],)) == 2
    assert (
        _scalar("SELECT COUNT(*) FROM contract_metadata WHERE task_id = %s", (fixture_ids["task_id"],)) == 1
    )


def test_the_clause_points_at_the_blocks_of_this_batch(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """``start_block_index`` / ``end_block_index`` 解析成了**本次写入的块主键**。

    下标 0/1 必须落在 order_index 0/1 那两个块上，2/2 落在第三个块上 ——
    偏移一位就会把条款挂到错误的段落。
    """
    _post(client, fixture_ids["task_id"], _payload())

    rows = _query(
        "SELECT c.clause_type, s.order_index, e.order_index, c.char_start_global, c.char_end_global "
        "FROM clause c "
        "JOIN document_block s ON c.start_block_id = s.id "
        "JOIN document_block e ON c.end_block_id = e.id "
        "WHERE c.task_id = %s ORDER BY c.id",
        (fixture_ids["task_id"],),
    )
    assert rows[0][:3] == ("IP", 0, 1)
    assert rows[1][:3] == ("LIABILITY", 2, 2)
    # char_* 未显式给出 → 取首/末块的全局区间
    assert rows[0][3] == 0
    assert rows[0][4] == len(_TEXTS[0]) + 1 + len(_TEXTS[1])


def test_the_metadata_points_at_a_block_of_this_batch(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    _post(client, fixture_ids["task_id"], _payload())

    row = _query(
        "SELECT b.order_index FROM contract_metadata m "
        "JOIN document_block b ON m.source_block_id = b.id "
        "WHERE m.task_id = %s",
        (fixture_ids["task_id"],),
    )[0]

    assert row[0] == 1


def test_the_sourceless_block_fields_follow_the_documented_rule(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """``raw_text`` / ``char_*_in_block`` 没有真实来源时按文档口径补齐。

    ⚠️ ``raw_text`` 存的是**归一化后的文本**（Parser 未保留原文）——
    这里把这个已知偏差固定下来，不让它悄悄变成"看起来有原文"。
    """
    _post(client, fixture_ids["task_id"], _payload())

    block = _blocks_of(fixture_ids["contract_id"])[0]
    text = block[3]
    assert block[4] == text, "raw_text 当前等于 text（Parser 未保留未归一化原文）"
    assert (block[5], block[6]) == (0, len(text))
    assert block[9] == "PARAGRAPH", "locator_type 由附件扩展名派生（docx → PARAGRAPH）"


def test_an_explicit_value_wins_over_the_derived_one(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """调用方给了就用它的 —— 派生只是"没给"时的兜底。"""
    payload = _payload()
    payload["blocks"][0]["raw_text"] = "原始文本（未归一化）"
    payload["blocks"][0]["char_start_in_block"] = 3
    payload["blocks"][0]["char_end_in_block"] = 7

    _post(client, fixture_ids["task_id"], payload)

    block = _blocks_of(fixture_ids["contract_id"])[0]
    assert block[4] == "原始文本（未归一化）"
    assert (block[5], block[6]) == (3, 7)


# --------------------------------------------------------------------------- #
# 3：任务级幂等
# --------------------------------------------------------------------------- #
def test_a_second_write_for_the_same_task_is_rejected(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    first = _post(client, fixture_ids["task_id"], _payload())
    second = _post(client, fixture_ids["task_id"], _payload())

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == ErrorCode.DOCUMENT_ALREADY_PERSISTED.value
    assert len(_blocks_of(fixture_ids["contract_id"])) == 3, "既不追加也不覆盖"


def test_a_failed_write_written_as_an_empty_batch_still_occupies_the_task(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """``FAILED`` 一个块都不写 —— 若只靠行数检查，"失败过一次"就会留下缺口。"""
    first = _post(
        client, fixture_ids["task_id"], _payload(parse_status="FAILED", blocks=[], clauses=[], metadata=[])
    )

    assert first.status_code == 201
    assert first.json()["blocks_created"] == 0
    assert _post(client, fixture_ids["task_id"], _payload()).status_code == 409


# --------------------------------------------------------------------------- #
# 4 / 10：文件级复用
# --------------------------------------------------------------------------- #
def test_the_same_file_is_not_given_a_second_set_of_blocks(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """同一文件第二个任务：**复用块**，条款与元数据各自独立。"""
    contract_id, file_id = fixture_ids["contract_id"], fixture_ids["file_id"]
    second_task = _insert_task(contract_id, file_id)

    _post(client, fixture_ids["task_id"], _payload())
    response = _post(client, second_task, _payload())

    assert response.status_code == 201
    assert response.json()["blocks_created"] == 0
    assert response.json()["blocks_reused"] == 3
    assert len(_blocks_of(contract_id)) == 3, "块没有翻倍"

    # 条款与元数据是两个任务各自的
    assert _scalar("SELECT COUNT(*) FROM clause WHERE task_id = %s", (fixture_ids["task_id"],)) == 2
    assert _scalar("SELECT COUNT(*) FROM clause WHERE task_id = %s", (second_task,)) == 2
    # 第二个任务的条款指向的还是**同一批**块
    assert (
        _scalar("SELECT COUNT(DISTINCT c.start_block_id) FROM clause c WHERE c.task_id = %s", (second_task,))
        == 2
    )


def test_a_second_task_on_the_same_file_may_restate_the_same_parse_status(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """同值写入是幂等的，必须放行 —— 否则文件复用这条已批准的能力被门禁打死。"""
    contract_id, file_id = fixture_ids["contract_id"], fixture_ids["file_id"]
    second_task = _insert_task(contract_id, file_id)

    _post(client, fixture_ids["task_id"], _payload())
    second = _post(client, second_task, _payload())

    assert second.status_code == 201
    assert second.json()["parse_status"] == "PARSED"


def test_a_contradictory_parse_status_is_rejected(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """已 ``PARSED`` 的文件不允许被改写成 ``FAILED`` —— 原有块必须原样不动。"""
    contract_id, file_id = fixture_ids["contract_id"], fixture_ids["file_id"]
    second_task = _insert_task(contract_id, file_id)
    _post(client, fixture_ids["task_id"], _payload())
    before = _blocks_of(contract_id)

    response = _post(client, second_task, _payload(parse_status="FAILED", blocks=[], clauses=[], metadata=[]))

    assert response.status_code == 409
    assert response.json()["code"] == ErrorCode.PARSE_STATUS_ALREADY_FINAL.value
    assert _scalar("SELECT parse_status FROM contract_file WHERE id = %s", (file_id,)) == "PARSED"
    assert _blocks_of(contract_id) == before, "原有块一个字段都不该变"
    assert _scalar("SELECT COUNT(*) FROM clause WHERE task_id = %s", (second_task,)) == 0
    assert _scalar("SELECT current_stage FROM review_task WHERE id = %s", (second_task,)) == "UPLOADED", (
        "被拒绝的请求不该推进阶段"
    )


def test_a_failed_file_can_be_reparsed_successfully_by_a_later_task(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """``FAILED`` **不是**永久终态：后续任务重新解析成功时，文件被升级为 ``PARSED``。

    这是必须放行的一条 —— 失败的那次没有落任何块，库里还是空的。
    若禁止升级，"解析器修好了"之后这个文件就**永远无法持久化**了。

    同时它把"块是文件级、条款/元数据是任务级"这条归属规则走通了一整遍：
    块由新任务首次创建，条款/元数据只属于新任务。
    """
    contract_id, file_id = fixture_ids["contract_id"], fixture_ids["file_id"]

    # 第一次：解析失败，一个块都不落
    first = _post(
        client, fixture_ids["task_id"], _payload(parse_status="FAILED", blocks=[], clauses=[], metadata=[])
    )
    assert first.status_code == 201
    assert _scalar("SELECT parse_status FROM contract_file WHERE id = %s", (file_id,)) == "FAILED"
    assert _blocks_of(contract_id) == []

    # 第二次：新任务，解析成功
    retry_task = _insert_task(contract_id, file_id)
    second = _post(client, retry_task, _payload())

    assert second.status_code == 201
    assert second.json()["parse_status"] == "PARSED"
    assert second.json()["blocks_created"] == 3, "块由这次任务首次创建"
    assert second.json()["blocks_reused"] == 0
    assert _scalar("SELECT parse_status FROM contract_file WHERE id = %s", (file_id,)) == "PARSED"

    assert len(_blocks_of(contract_id)) == 3
    assert _scalar("SELECT COUNT(*) FROM clause WHERE task_id = %s", (retry_task,)) == 2
    assert _scalar("SELECT COUNT(*) FROM contract_metadata WHERE task_id = %s", (retry_task,)) == 1
    # 失败的那个任务仍然什么都没有 —— 块是文件级，但条款/元数据不是
    assert _scalar("SELECT COUNT(*) FROM clause WHERE task_id = %s", (fixture_ids["task_id"],)) == 0
    # 重试任务的条款挂在真实块上
    assert (
        _scalar(
            "SELECT COUNT(*) FROM clause c WHERE c.task_id = %s AND c.start_block_id IS NOT NULL",
            (retry_task,),
        )
        == 2
    )


def test_a_mismatched_block_structure_is_rejected(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """同一文件被解析出两套结构（例如 Parser 版本变了）→ 拒绝，不按位置硬套。"""
    contract_id, file_id = fixture_ids["contract_id"], fixture_ids["file_id"]
    second_task = _insert_task(contract_id, file_id)
    _post(client, fixture_ids["task_id"], _payload())

    payload = _payload()
    payload["blocks"] = payload["blocks"][:2]  # 少一块
    payload["clauses"] = []
    payload["metadata"] = []

    response = _post(client, second_task, payload)

    assert response.status_code == 409
    assert response.json()["code"] == ErrorCode.DOCUMENT_BLOCKS_CONFLICT.value


# --------------------------------------------------------------------------- #
# 5 / 6 / 7：空文档、失败、非法状态
# --------------------------------------------------------------------------- #
def test_an_empty_document_is_persisted_as_parsed_with_no_blocks(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """空文档是**数据问题**，不是解析失败：``PARSED`` + 0 块（不新增 EMPTY 状态）。"""
    response = _post(client, fixture_ids["task_id"], _payload(blocks=[], clauses=[], metadata=[]))

    assert response.status_code == 201
    assert response.json()["parse_status"] == "PARSED"
    assert response.json()["blocks_created"] == 0
    assert (
        _scalar("SELECT parse_status FROM contract_file WHERE id = %s", (fixture_ids["file_id"],)) == "PARSED"
    )


def test_a_failed_parse_is_recorded(client: TestClient, fixture_ids: dict[str, int]) -> None:
    response = _post(
        client, fixture_ids["task_id"], _payload(parse_status="FAILED", blocks=[], clauses=[], metadata=[])
    )

    assert response.status_code == 201
    assert (
        _scalar("SELECT parse_status FROM contract_file WHERE id = %s", (fixture_ids["file_id"],)) == "FAILED"
    )
    assert _blocks_of(fixture_ids["contract_id"]) == []


@pytest.mark.parametrize("bad", ["PENDING", "PARSING", "DONE"])
def test_an_illegal_parse_status_is_rejected(
    client: TestClient, fixture_ids: dict[str, int], bad: str
) -> None:
    response = _post(client, fixture_ids["task_id"], _payload(parse_status=bad))

    assert response.status_code == 422
    assert _blocks_of(fixture_ids["contract_id"]) == [], "校验失败不该留下任何东西"


def test_an_out_of_range_block_reference_is_rejected(client: TestClient, fixture_ids: dict[str, int]) -> None:
    payload = _payload()
    payload["clauses"][0]["end_block_index"] = 99

    response = _post(client, fixture_ids["task_id"], payload)

    assert response.status_code == 422
    assert _blocks_of(fixture_ids["contract_id"]) == []


def test_a_missing_task_is_a_404(client: TestClient) -> None:
    response = _post(client, 2**40, _payload())

    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.TASK_NOT_FOUND.value


# --------------------------------------------------------------------------- #
# 8：事务边界
# --------------------------------------------------------------------------- #
def test_a_failure_after_the_blocks_are_inserted_rolls_everything_back(
    fixture_ids: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**核心**：块已经 flush 出去了，后面任何一步炸掉都必须把它们一起回滚。

    故障注入在"块已插入、条款还没写"之间 —— 那正是半成品最可能出现的位置。
    若不注入，就无法构造出"已经写过一部分"的真实失败（请求体里的越界引用
    在进事务之前就被 pydantic 拦掉了）。
    """
    import app.services.document_persistence as service

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("注入的故障：写条款时崩溃")

    monkeypatch.setattr(service, "_insert_clauses", _boom)

    with TestClient(app, raise_server_exceptions=False) as failing_client:
        response = _post(failing_client, fixture_ids["task_id"], _payload())

    assert response.status_code == 500
    assert _blocks_of(fixture_ids["contract_id"]) == [], "块必须跟着回滚"
    assert (
        _scalar("SELECT parse_status FROM contract_file WHERE id = %s", (fixture_ids["file_id"],))
        == "PENDING"
    )
    assert (
        _scalar("SELECT current_stage FROM review_task WHERE id = %s", (fixture_ids["task_id"],))
        == "UPLOADED"
    )


def test_the_stage_and_parse_status_are_updated_in_the_same_transaction(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    _post(client, fixture_ids["task_id"], _payload())

    assert (
        _scalar("SELECT current_stage FROM review_task WHERE id = %s", (fixture_ids["task_id"],)) == "CLAUSED"
    )
    assert _scalar("SELECT status FROM review_task WHERE id = %s", (fixture_ids["task_id"],)) == "pending"
    assert (
        _scalar("SELECT parse_status FROM contract_file WHERE id = %s", (fixture_ids["file_id"],)) == "PARSED"
    )


def test_the_task_stage_advances_without_touching_the_state_machine(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """与 P9-10 同口径：只推进 ``current_stage``，不动 ``status`` 与 ``finished_at``。"""
    _post(client, fixture_ids["task_id"], _payload())

    row = _query(
        "SELECT status, current_stage, finished_at FROM review_task WHERE id = %s",
        (fixture_ids["task_id"],),
    )[0]
    assert row == ("pending", "CLAUSED", None)


# --------------------------------------------------------------------------- #
# 9：并发
# --------------------------------------------------------------------------- #
@pytest.fixture
async def asgi_client() -> AsyncIterator[httpx.AsyncClient]:
    """直连 ASGI 的异步客户端，**并在同一个事件循环里释放连接池**（同 P9-10）。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://backend.test") as client:
        yield client
    await _dispose()


async def test_concurrent_writes_only_one_wins(
    asgi_client: httpx.AsyncClient, fixture_ids: dict[str, int]
) -> None:
    """同一个任务的两个并发请求：一个成功、一个 409，库里只能有一份文档。

    与 P9-10 同一形状：两条门禁都是"先读后写"，靠 ``review_task`` 行锁串行化。
    """
    task_id = fixture_ids["task_id"]

    responses = list(
        await asyncio.gather(
            asgi_client.post(f"/api/v1/review-tasks/{task_id}/document", json=_payload()),
            asgi_client.post(f"/api/v1/review-tasks/{task_id}/document", json=_payload()),
        )
    )

    assert sorted(r.status_code for r in responses) == [201, 409]
    rejected = next(r for r in responses if r.status_code == 409)
    assert rejected.json()["code"] == ErrorCode.DOCUMENT_ALREADY_PERSISTED.value
    assert len(_blocks_of(fixture_ids["contract_id"])) == 3, "块不能翻倍"
    assert _scalar("SELECT COUNT(*) FROM clause WHERE task_id = %s", (task_id,)) == 2
