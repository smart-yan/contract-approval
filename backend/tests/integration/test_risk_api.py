"""``POST /api/v1/review-tasks/{task_id}/risks`` 的集成测试（**需要真实 MySQL**）。

为什么放在 integration
---------------------
这里断言的全是**数据库层面**的事实：外键真的解析成了 id、事务真的回滚了、
重复写入真的被唯一性以外的机制挡住了、任务状态与风险在同一事务里改掉。
SQLite 上跑不出同样的语义。

隔离方式
--------
本模块自己造 ``contract`` / ``contract_file`` / ``review_task``，
并以 ``IT-RISK-`` 前缀命名，收尾按外键依赖的反序删除。
租户数据（seed 的 PURCHASE 规则集）只**读**不写。
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

PREFIX = "IT-RISK-"
CONTRACT_TYPE = "PURCHASE"
RULE_CODE = "IP_OWNER_SUPPLIER_001"


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
    """按外键依赖的反序清理本模块造的数据。"""
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
    contract_no = f"{PREFIX}{uuid.uuid4().hex[:12]}"
    _query(
        "INSERT INTO contract (contract_no, title, contract_type, source, status, created_at, updated_at) "
        "VALUES (%s, %s, %s, 'UPLOAD', 'PENDING', NOW(3), NOW(3))",
        (contract_no, "风险写入测试合同", CONTRACT_TYPE),
    )
    return _scalar("SELECT MAX(id) FROM contract")


def _insert_file(contract_id: int, *, file_ext: str = "docx") -> int:
    _query(
        "INSERT INTO contract_file "
        "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
        " created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 0, 'PARSED', NOW(3), NOW(3))",
        (
            contract_id,
            f"contract.{file_ext}",
            file_ext,
            1024,
            uuid.uuid4().hex + uuid.uuid4().hex,
            f"{contract_id}/contract.{file_ext}",
        ),
    )
    return _scalar("SELECT MAX(id) FROM contract_file")


def _insert_task(contract_id: int, file_id: int, *, stage: str = "CLAUSED") -> int:
    """建一个可写风险的任务。

    ⚠️ 默认阶段是 ``CLAUSED`` 而不是 ``UPLOADED``：风险写入**要求文档层先落库**
    （``risk_item.clause_id`` 要靠 ``clause`` 解析），这是服务端强制的契约。
    ``UPLOADED`` 那一侧由 ``test_risks_cannot_be_written_before_the_document_layer`` 专门覆盖。
    """
    _query(
        "INSERT INTO review_task "
        "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
        " max_retry, idempotency_key, created_at, updated_at) "
        "VALUES (%s, %s, 'pending', %s, 0, 0, 0, 0, 3, %s, NOW(3), NOW(3))",
        (contract_id, file_id, stage, uuid.uuid4().hex + uuid.uuid4().hex),
    )
    return _scalar("SELECT MAX(id) FROM review_task")


def _insert_block(contract_id: int, file_id: int, *, order_index: int, paragraph_index: int) -> int:
    _query(
        "INSERT INTO document_block "
        "(contract_id, file_id, order_index, block_type, paragraph_index, locator_type, text, raw_text, "
        " char_start_in_block, char_end_in_block, char_start_global, char_end_global, created_at, updated_at) "
        "VALUES (%s, %s, %s, 'PARAGRAPH', %s, 'PARAGRAPH', %s, %s, 0, 10, %s, %s, NOW(3), NOW(3))",
        (
            contract_id,
            file_id,
            order_index,
            paragraph_index,
            f"段落 {paragraph_index}",
            f"段落 {paragraph_index}",
            order_index * 100,
            order_index * 100 + 10,
        ),
    )
    return _scalar("SELECT MAX(id) FROM document_block")


def _insert_clause(contract_id: int, task_id: int, *, start_block_id: int, end_block_id: int) -> int:
    _query(
        "INSERT INTO clause "
        "(contract_id, task_id, clause_type, start_block_id, end_block_id, char_start_global, "
        " char_end_global, text, extract_method, created_at, updated_at) "
        "VALUES (%s, %s, 'IP', %s, %s, 0, 100, '条款全文', 'RULE', NOW(3), NOW(3))",
        (contract_id, task_id, start_block_id, end_block_id),
    )
    return _scalar("SELECT MAX(id) FROM clause")


def _rule_id() -> int | None:
    return _scalar(
        "SELECT r.id FROM review_rule r JOIN review_rule_set s ON r.rule_set_id = s.id "
        "WHERE s.contract_type = %s AND s.is_active = 1 AND r.rule_code = %s "
        "ORDER BY s.id DESC LIMIT 1",
        (CONTRACT_TYPE, RULE_CODE),
    )


@pytest.fixture
def fixture_ids() -> Iterator[dict[str, int]]:
    """一套最小可用的 合同 / 附件 / 任务。"""
    contract_id = _insert_contract()
    file_id = _insert_file(contract_id)
    task_id = _insert_task(contract_id, file_id)
    yield {"contract_id": contract_id, "file_id": file_id, "task_id": task_id}


# --------------------------------------------------------------------------- #
# 请求构造
# --------------------------------------------------------------------------- #
def _risk(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "risk_code": RULE_CODE,
        "risk_title": "知识产权归属相对方",
        "dimension": "知识产权",
        "risk_level": "HIGH",
        "source": "RULE",
        "reason": "命中规则关键词：「知识产权归乙方」",
        "legal_basis": "《民法典》第 843 条",
        "quote": "知识产权归乙方",
        "paragraph_index": 23,
        "anchor_method": None,
    }
    payload.update(overrides)
    return payload


def _post(client: TestClient, task_id: int, risks: list[dict[str, Any]]):
    return client.post(f"/api/v1/review-tasks/{task_id}/risks", json={"risks": risks})


def _risks_of(task_id: int) -> list[tuple]:
    return _query(
        "SELECT risk_title, dimension, risk_level, source, risk_code, rule_id, clause_id, "
        "       original_text, paragraph_index, locator_type, anchor_method, review_status "
        "FROM risk_item WHERE task_id = %s ORDER BY id",
        (task_id,),
    )


# --------------------------------------------------------------------------- #
# 1 / 2 / 3：三种来源都能写入
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("source", ["RULE", "LLM", "RULE+LLM"])
def test_each_source_can_be_persisted(client: TestClient, fixture_ids: dict[str, int], source: str) -> None:
    risk = _risk(source=source)
    if source == "LLM":
        risk["risk_code"] = None  # 纯 LLM 风险没有规则编码

    response = _post(client, fixture_ids["task_id"], [risk])

    assert response.status_code == 201
    body = response.json()
    assert body["persisted"] == 1
    assert _scalar("SELECT source FROM risk_item WHERE task_id = %s", (fixture_ids["task_id"],)) == source


def test_a_pure_llm_risk_has_no_rule_id(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """``risk_code`` 为空 → ``rule_id`` 为空。这不是"解析失败"，是**本来就没有规则**。"""
    response = _post(client, fixture_ids["task_id"], [_risk(risk_code=None, source="LLM")])

    assert response.status_code == 201
    assert _scalar("SELECT rule_id FROM risk_item WHERE task_id = %s", (fixture_ids["task_id"],)) is None


# --------------------------------------------------------------------------- #
# 4：quote → original_text
# --------------------------------------------------------------------------- #
def test_the_quote_lands_in_the_original_text_column(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """⚠️ 列里必须是**命中片段**，不是整段原文。

    Agent 的 ``AgentRiskItem.original_text`` 指段落原文、``quote`` 指命中片段；
    Backend 这一列的名字虽然叫 ``original_text``，要的却是**后者**。
    """
    _post(client, fixture_ids["task_id"], [_risk(quote="知识产权归乙方")])

    assert (
        _scalar("SELECT original_text FROM risk_item WHERE task_id = %s", (fixture_ids["task_id"],))
        == "知识产权归乙方"
    )


# --------------------------------------------------------------------------- #
# 5 / 6：服务端决定的字段
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("file_ext", "expected"), [("docx", "PARAGRAPH"), ("pdf", "PAGE")])
def test_the_locator_type_is_derived_from_the_attachment(
    client: TestClient, file_ext: str, expected: str
) -> None:
    contract_id = _insert_contract()
    file_id = _insert_file(contract_id, file_ext=file_ext)
    task_id = _insert_task(contract_id, file_id)

    _post(client, task_id, [_risk()])

    assert _scalar("SELECT locator_type FROM risk_item WHERE task_id = %s", (task_id,)) == expected


def test_the_review_status_is_always_pending(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """AI 刚产出的风险一律 ``PENDING`` —— 客户端**无法**让它变成"已确认"。"""
    _post(client, fixture_ids["task_id"], [_risk()])

    assert (
        _scalar("SELECT review_status FROM risk_item WHERE task_id = %s", (fixture_ids["task_id"],))
        == "PENDING"
    )


def test_the_client_cannot_choose_server_side_fields(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """请求里塞了这些键也不会被采纳（契约里根本没有它们）。"""
    risk = _risk()
    risk.update(
        {
            "review_status": "CONFIRMED",
            "locator_type": "PAGE",
            "rule_id": 999999,
            "clause_id": 999999,
            "task_id": 1,
            "contract_id": 1,
        }
    )

    response = _post(client, fixture_ids["task_id"], [risk])

    assert response.status_code == 201
    row = _risks_of(fixture_ids["task_id"])[0]
    assert row[11] == "PENDING", "review_status 只能是服务端写的 PENDING"
    assert row[9] == "PARAGRAPH", "locator_type 由附件扩展名派生，不是请求里那个 PAGE"
    assert row[5] != 999999 and row[6] is None


# --------------------------------------------------------------------------- #
# 7：rule_code → rule_id
# --------------------------------------------------------------------------- #
def test_the_rule_code_is_resolved_to_a_rule_id(client: TestClient, fixture_ids: dict[str, int]) -> None:
    expected = _rule_id()
    assert expected is not None, "seed 里应当有这条采购规则"

    _post(client, fixture_ids["task_id"], [_risk(risk_code=RULE_CODE)])

    assert _scalar("SELECT rule_id FROM risk_item WHERE task_id = %s", (fixture_ids["task_id"],)) == expected


# --------------------------------------------------------------------------- #
# 8：paragraph_index → clause_id
# --------------------------------------------------------------------------- #
def test_the_paragraph_is_resolved_to_a_clause(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """段落落在条款区间内 → 写入该条款的 id。"""
    contract_id, file_id = fixture_ids["contract_id"], fixture_ids["file_id"]
    start = _insert_block(contract_id, file_id, order_index=1, paragraph_index=20)
    end = _insert_block(contract_id, file_id, order_index=2, paragraph_index=25)
    clause_id = _insert_clause(contract_id, fixture_ids["task_id"], start_block_id=start, end_block_id=end)

    _post(client, fixture_ids["task_id"], [_risk(paragraph_index=23)])

    assert (
        _scalar("SELECT clause_id FROM risk_item WHERE task_id = %s", (fixture_ids["task_id"],)) == clause_id
    )


def test_a_paragraph_outside_every_clause_stays_null(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """条款存在、但这一段不属于任何条款 → ``clause_id`` 为空（事实，不是猜）。"""
    contract_id, file_id = fixture_ids["contract_id"], fixture_ids["file_id"]
    start = _insert_block(contract_id, file_id, order_index=1, paragraph_index=20)
    end = _insert_block(contract_id, file_id, order_index=2, paragraph_index=25)
    _insert_clause(contract_id, fixture_ids["task_id"], start_block_id=start, end_block_id=end)

    response = _post(client, fixture_ids["task_id"], [_risk(paragraph_index=99)])

    assert response.status_code == 201
    assert _scalar("SELECT clause_id FROM risk_item WHERE task_id = %s", (fixture_ids["task_id"],)) is None


# --------------------------------------------------------------------------- #
# 9：非法 rule_code → 整批拒绝
# --------------------------------------------------------------------------- #
def test_an_unknown_rule_code_rejects_the_whole_batch(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """**绝不静默写 NULL** —— 写 NULL 会被读成"这条风险不来自任何规则"。"""
    response = _post(
        client,
        fixture_ids["task_id"],
        [_risk(risk_code=RULE_CODE), _risk(risk_code="NO_SUCH_RULE_999")],
    )

    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.RULE_NOT_FOUND.value
    assert _risks_of(fixture_ids["task_id"]) == [], "一条都不该写进去"


# --------------------------------------------------------------------------- #
# 10：幂等
# --------------------------------------------------------------------------- #
def test_a_second_write_is_rejected(client: TestClient, fixture_ids: dict[str, int]) -> None:
    first = _post(client, fixture_ids["task_id"], [_risk()])
    second = _post(client, fixture_ids["task_id"], [_risk(risk_title="另一条")])

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == ErrorCode.TASK_ALREADY_PERSISTED.value
    assert len(_risks_of(fixture_ids["task_id"])) == 1, "既不追加也不覆盖"


def test_a_rewrite_after_manual_review_is_rejected(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """已有人工复核结果时更要拒绝 —— 覆盖会把法务的判断静默抹掉。"""
    _post(client, fixture_ids["task_id"], [_risk()])
    _query(
        "UPDATE risk_item SET review_status = 'CONFIRMED', review_comment = '法务已确认' WHERE task_id = %s",
        (fixture_ids["task_id"],),
    )

    second = _post(client, fixture_ids["task_id"], [_risk()])

    assert second.status_code == 409
    row = _query(
        "SELECT review_status, review_comment FROM risk_item WHERE task_id = %s", (fixture_ids["task_id"],)
    )[0]
    assert row == ("CONFIRMED", "法务已确认"), "人工复核结果原样保留"


def test_an_empty_batch_occupies_the_task(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """``risks=[]`` **也是一次真实的审查结果**（"这次审查没有风险"）。

    因此它同样占用这个任务：阶段被推进，之后任何写入请求都必须被拒绝 ——
    哪怕它一行风险都没写。

    ⚠️ 这一条靠的是**阶段门禁**（``current_stage == REVIEWED``）。
    只查 ``risk_item`` 行数是拦不住的：空批次写 0 行，行数检查永远不触发，
    于是会出现"已 REVIEWED 却还能再写"的缺口。
    """
    # 1) 空批次正常写入
    first = _post(client, fixture_ids["task_id"], [])
    assert first.status_code == 201
    assert first.json()["persisted"] == 0

    # 2) 阶段被推进，但状态机与结束时刻都不动
    row = _query(
        "SELECT status, current_stage, finished_at FROM review_task WHERE id = %s",
        (fixture_ids["task_id"],),
    )[0]
    assert row[0] == "pending", "不绕过迁移矩阵"
    assert row[1] == "REVIEWED"
    assert row[2] is None, "任务尚未结束，不该有结束时刻"

    # 3) 后续非空批次被拒绝
    second = _post(client, fixture_ids["task_id"], [_risk()])
    assert second.status_code == 409
    assert second.json()["code"] == ErrorCode.TASK_ALREADY_PERSISTED.value

    # 4) 后续空批次同样被拒绝（幂等与"写了什么"无关）
    third = _post(client, fixture_ids["task_id"], [])
    assert third.status_code == 409
    assert third.json()["code"] == ErrorCode.TASK_ALREADY_PERSISTED.value

    # 5) 被拒绝的两次都没有留下任何风险
    assert _risks_of(fixture_ids["task_id"]) == []


# --------------------------------------------------------------------------- #
# 11 / 12 / 13：原子性与事务
# --------------------------------------------------------------------------- #
def test_a_multi_row_batch_is_written_atomically(client: TestClient, fixture_ids: dict[str, int]) -> None:
    risks = [
        _risk(paragraph_index=1),
        _risk(paragraph_index=2, risk_title="第二条"),
        _risk(paragraph_index=3, risk_title="第三条"),
    ]

    response = _post(client, fixture_ids["task_id"], risks)

    assert response.status_code == 201
    assert response.json()["persisted"] == 3
    assert len(_risks_of(fixture_ids["task_id"])) == 3


def test_a_failure_in_the_middle_rolls_everything_back(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """**核心**：最后一条非法 → 前两条也必须一起回滚，不能留下半批风险。

    半批比没有更糟 —— 人工看到的是一份"看起来完整、实则缺了几条"的清单，
    而且没有任何信号提示它不完整。
    """
    risks = [
        _risk(paragraph_index=1),
        _risk(paragraph_index=2, risk_title="第二条"),
        _risk(paragraph_index=3, risk_code="NO_SUCH_RULE_999"),
    ]

    response = _post(client, fixture_ids["task_id"], risks)

    assert response.status_code == 404
    assert _risks_of(fixture_ids["task_id"]) == [], "一条都不能留下"


def test_the_task_stage_is_updated_in_the_same_transaction(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """成功：风险与**阶段**一起生效，但**状态机与结束时刻都不动**。

    ``PENDING → COMPLETED`` 不在 §6.1 的 ``TASK_STATUS_TRANSITIONS`` 里，
    而那套矩阵是唯一权威；逐级走 PARSING → REVIEWING 又会伪造 Backend 侧
    从未发生过的生命周期。因此这一阶段能表达的只有 ``current_stage`` 这一句：
    "Agent 已完成风险审查并持久化"。
    """
    response = _post(client, fixture_ids["task_id"], [_risk()])

    assert response.status_code == 201
    assert response.json()["task_stage"] == "REVIEWED"
    assert response.json()["task_status"] == "pending", "状态机没有被推进"
    row = _query(
        "SELECT status, current_stage, finished_at FROM review_task WHERE id = %s",
        (fixture_ids["task_id"],),
    )[0]
    assert row[0] == "pending", "不绕过迁移矩阵"
    assert row[1] == "REVIEWED"
    assert row[2] is None, "任务尚未结束，不该有结束时刻"


def test_the_task_is_not_given_a_verdict(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """综合等级与结论由**评分**产出，本阶段不生成（Agent 侧同样没有评分器）。

    顺手填一个"最高等级"等于在这个字段里写下一个没人负责的结论。
    """
    _post(client, fixture_ids["task_id"], [_risk(risk_level="HIGH")])

    row = _query(
        "SELECT risk_level_final, conclusion FROM review_task WHERE id = %s",
        (fixture_ids["task_id"],),
    )[0]
    assert row == (None, None)


def test_a_rolled_back_batch_leaves_the_task_untouched(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """失败：阶段**不能**被推进 —— 否则就是"没有风险"与"没写成"混为一谈。"""
    response = _post(client, fixture_ids["task_id"], [_risk(risk_code="NO_SUCH_RULE_999")])

    assert response.status_code == 404
    assert _risks_of(fixture_ids["task_id"]) == [], "风险一条都不残留"
    row = _query(
        "SELECT status, current_stage, finished_at FROM review_task WHERE id = %s",
        (fixture_ids["task_id"],),
    )[0]
    assert row == ("pending", "CLAUSED", None)


# --------------------------------------------------------------------------- #
# 其它
# --------------------------------------------------------------------------- #
def test_risks_cannot_be_written_before_the_document_layer(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """**前置门禁**：文档层没落库之前，风险一律不写。

    没有这道门禁就会出现一条不可逆的坏终局：风险先落库（``clause_id`` 全为 NULL），
    阶段被推到 ``REVIEWED``，于是文档层**永久**无法补写 ——
    库里留下一批"永远挂不上条款"的风险，而且不会有任何报错。
    只靠"Agent 会按顺序调用"规避是不行的：顺序是契约，必须在服务端强制。
    """
    contract_id, file_id = fixture_ids["contract_id"], fixture_ids["file_id"]
    fresh_task = _insert_task(contract_id, file_id, stage="UPLOADED")

    response = _post(client, fresh_task, [_risk()])

    assert response.status_code == 409
    assert response.json()["code"] == ErrorCode.DOCUMENT_NOT_PERSISTED.value
    assert _risks_of(fresh_task) == [], "一条风险都不许写进去"
    assert _scalar("SELECT current_stage FROM review_task WHERE id = %s", (fresh_task,)) == "UPLOADED", (
        "被拒绝的请求不推进阶段"
    )
    assert _scalar("SELECT COUNT(*) FROM document_block WHERE contract_id = %s", (contract_id,)) == 0, (
        "文档层仍然可以补写"
    )


def test_the_document_layer_can_still_be_written_after_a_rejected_risk_attempt(
    client: TestClient, fixture_ids: dict[str, int]
) -> None:
    """被前置门禁拒绝**不会**堵死文档层 —— 补上文档层之后风险照常可写。

    这正是这道门禁要保住的东西：拒绝只发生在"顺序错了"的时候，
    纠正顺序之后整条流水线仍然走得通。
    """
    contract_id, file_id = fixture_ids["contract_id"], fixture_ids["file_id"]
    fresh_task = _insert_task(contract_id, file_id, stage="UPLOADED")

    assert _post(client, fresh_task, [_risk()]).status_code == 409

    document = client.post(
        f"/api/v1/review-tasks/{fresh_task}/document",
        json={
            "parse_status": "PARSED",
            "blocks": [
                {
                    "order_index": 0,
                    "paragraph_index": 23,
                    "block_type": "PARAGRAPH",
                    "text": "本项目产生的知识产权归乙方所有。",
                    "char_start_global": 0,
                    "char_end_global": 16,
                }
            ],
            "clauses": [
                {
                    "clause_type": "IP",
                    "text": "本项目产生的知识产权归乙方所有。",
                    "extract_method": "RULE",
                    "start_block_index": 0,
                    "end_block_index": 0,
                }
            ],
            "metadata": [],
        },
    )
    assert document.status_code == 201
    assert document.json()["current_stage"] == "CLAUSED"

    assert _post(client, fresh_task, [_risk()]).status_code == 201


def test_a_missing_task_is_a_404(client: TestClient) -> None:
    response = _post(client, 2**40, [_risk()])

    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.TASK_NOT_FOUND.value


def test_an_empty_flattened_row_set_is_reported(client: TestClient, fixture_ids: dict[str, int]) -> None:
    """响应里的计数与库里真实条数一致（不是一个写死的 1）。"""
    body = _post(client, fixture_ids["task_id"], [_risk(), _risk(risk_title="第二条")]).json()

    assert body["persisted"] == len(_risks_of(fixture_ids["task_id"])) == 2


# --------------------------------------------------------------------------- #
# 并发
# --------------------------------------------------------------------------- #
@pytest.fixture
async def asgi_client() -> AsyncIterator[httpx.AsyncClient]:
    """直连 ASGI 的异步客户端，**并在同一个事件循环里释放连接池**。

    为什么要在这里显式 ``dispose_engine()``：引擎是懒加载的全局单例，会绑在
    **第一次使用它的那个事件循环**上。本模块其余用例走 ``TestClient``（自带一个
    portal 循环），收尾时由 ``_cleanup`` 用 ``asyncio.run()`` 再起一个循环去释放 ——
    那种"跨循环释放"对本用例的异步循环不成立，连接会在 GC 时才被回收，
    于是抛出 ``PytestUnraisableExceptionWarning``。在自己的循环里关掉即可。

    （释放之后 ``_cleanup`` 的那次 ``asyncio.run(_dispose())`` 变成空操作 ——
    ``dispose_engine()`` 对已释放的引擎是幂等的。）
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://backend.test") as client:
        yield client
    await _dispose()


async def _post_twice_concurrently(
    client: httpx.AsyncClient, task_id: int, risks: list[dict[str, Any]]
) -> list[httpx.Response]:
    """在**同一个事件循环**里对同一个任务同时发两个持久化请求。

    用 ``httpx.ASGITransport`` 而不是 ``TestClient``：后者是同步阻塞的，
    两次调用会被串行化，"并发"就无从谈起。这里两个请求各自拿到独立的
    DB 会话（``session_scope`` 每次新开一个），因此真正会撞在数据库上。
    """
    return list(
        await asyncio.gather(
            client.post(f"/api/v1/review-tasks/{task_id}/risks", json={"risks": risks}),
            client.post(f"/api/v1/review-tasks/{task_id}/risks", json={"risks": risks}),
        )
    )


async def test_concurrent_writes_only_one_wins(
    asgi_client: httpx.AsyncClient, fixture_ids: dict[str, int]
) -> None:
    """同一个任务的两个并发请求：一个成功，另一个 409 —— 库里只能有一批风险。

    ⚠️ 这条用例**不保证**两个请求一定在关键点上交错（那需要人为插入屏障，
    而本项目没有为此引入任何测试基础设施）。它保证的是**最终状态**：
    只要 ``review_task`` 的行锁生效，无论两者如何交错，都只可能有一个赢家 ——
    这是 ``SELECT ... FOR UPDATE`` 给出的确定性，不是调度运气。
    """
    task_id = fixture_ids["task_id"]

    responses = await _post_twice_concurrently(asgi_client, task_id, [_risk()])

    assert sorted(r.status_code for r in responses) == [201, 409], (
        "必须是「一个成功、一个被拒」—— 两个 201 说明行锁没起作用"
    )
    rejected = next(r for r in responses if r.status_code == 409)
    assert rejected.json()["code"] == ErrorCode.TASK_ALREADY_PERSISTED.value

    assert len(_risks_of(task_id)) == 1, "库里只能有一批风险"
    row = _query("SELECT status, current_stage, finished_at FROM review_task WHERE id = %s", (task_id,))[0]
    assert row == ("pending", "REVIEWED", None)
