"""``GET /api/v1/review-tasks/{task_id}/workbench`` 的集成测试（**需要真实 MySQL**）。

这里断言的是**隔离**与**换算**——两件在 mock 里根本测不出来的事：

* ``document_block`` 是**文件级**的，段落号在每份文件里各自从 0 开始。
  只按 ``contract_id`` 查会把同一合同下另一份文件的段落捞进来，
  于是风险会高亮到**另一份文档的同号段落**，而且完全静默。
* ``clause`` / ``contract_metadata`` / ``risk_item`` 是**任务级**的。
  同一个文件可以跑多个任务（换规则集就新建），必须只取本任务的。

因此本模块的每个场景都刻意造出**干扰数据**：同一合同两个文件、同一文件两个任务。

隔离方式：自造 ``IT-WB-`` 前缀的合同，收尾按外键反序删除。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pymysql
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.config import get_settings
from app.main import app
from app.services.contract_query import progress_for_stage

PREFIX = "IT-WB-"


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


def _execute(sql: str, params: tuple = ()) -> int:
    """执行一条写语句，返回 lastrowid（或受影响行数）。"""
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
@dataclass(frozen=True)
class Scenario:
    """一个"带干扰"的完整场景。

    刻意造出三层干扰，任何一层隔离没做好都会被断言抓到：

    * **同合同两个文件**：``docx`` 与 ``pdf`` —— 两份的 ``paragraph_index`` 都从 0 起
    * **同文件两个任务**：``task_id`` 与 ``other_task_id`` —— 各自有条款与风险
    * **无来源块的元数据**：``source_block_id`` 为 NULL
    * **区间不可换算的条款**：``start/end_block_id`` 为 NULL
    """

    contract_id: int
    docx_file_id: int
    pdf_file_id: int
    task_id: int
    other_task_id: int
    docx_block_ids: tuple[int, ...]
    pdf_block_id: int
    clause_id: int
    other_clause_id: int


def _build_scenario() -> Scenario:
    contract_id = _execute(
        "INSERT INTO contract "
        "(contract_no, title, contract_type, source, status, our_party, counterparty, amount, currency, "
        " sign_date, dept, created_at, updated_at) "
        "VALUES (%s, '工作台测试合同', 'PURCHASE', 'UPLOAD', 'PENDING', '某某科技', '乙方公司', "
        "        1234.50, 'CNY', '2026-09-15', '法务部', NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:12]}",),
    )
    docx_file_id = _insert_file(contract_id, "contract.docx", ".docx", "PARSED")
    pdf_file_id = _insert_file(contract_id, "appendix.pdf", ".pdf", "PENDING")

    texts = ("第一条 知识产权", "本项目产生的知识产权归乙方所有。", "第二条 违约责任")
    docx_blocks = tuple(
        _insert_block(contract_id, docx_file_id, order_index=i, paragraph_index=i, text=text)
        for i, text in enumerate(texts)
    )
    # ⚠️ 另一份文件的段落号**也从 0 开始** —— 这正是要防的串数据
    pdf_block = _insert_block(
        contract_id, pdf_file_id, order_index=0, paragraph_index=0, text="另一份 PDF 的内容"
    )

    task_id = _insert_task(contract_id, docx_file_id, stage="CLAUSED")
    other_task_id = _insert_task(contract_id, docx_file_id, stage="REVIEWED")

    clause_id = _insert_clause(
        contract_id,
        task_id,
        clause_no="第一条",
        clause_type="IP",
        title="知识产权",
        start_block_id=docx_blocks[0],
        end_block_id=docx_blocks[1],
    )
    # 另一个任务也有条款 —— 不该出现在第一个任务的工作台里
    other_clause_id = _insert_clause(
        contract_id,
        other_task_id,
        clause_no="另一条",
        clause_type="OTHER",
        title=None,
        start_block_id=None,
        end_block_id=None,
    )
    _insert_clause(
        contract_id,
        task_id,
        clause_no=None,
        clause_type="LIABILITY",
        title="违约责任",
        start_block_id=None,
        end_block_id=None,
    )
    _insert_metadata(contract_id, task_id, field_key="counterparty_name", source_block_id=docx_blocks[1])
    _insert_metadata(contract_id, task_id, field_key="contract_amount", source_block_id=None)
    _insert_metadata(contract_id, other_task_id, field_key="other_task_field", source_block_id=docx_blocks[2])
    _insert_risk(
        task_id, contract_id, clause_id=clause_id, risk_code="IP_OWNER_SUPPLIER_001", paragraph_index=1
    )
    _insert_risk(
        other_task_id,
        contract_id,
        clause_id=other_clause_id,
        risk_code="LIAB_UNLIMITED_001",
        paragraph_index=2,
    )

    return Scenario(
        contract_id=contract_id,
        docx_file_id=docx_file_id,
        pdf_file_id=pdf_file_id,
        task_id=task_id,
        other_task_id=other_task_id,
        docx_block_ids=docx_blocks,
        pdf_block_id=pdf_block,
        clause_id=clause_id,
        other_clause_id=other_clause_id,
    )


def _insert_file(contract_id: int, name: str, ext: str, parse_status: str) -> int:
    return _execute(
        "INSERT INTO contract_file "
        "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
        " created_at, updated_at) "
        "VALUES (%s, %s, %s, 1024, %s, %s, 0, %s, NOW(3), NOW(3))",
        (contract_id, name, ext, uuid.uuid4().hex + uuid.uuid4().hex, f"{contract_id}/{name}", parse_status),
    )


def _insert_block(
    contract_id: int, file_id: int, *, order_index: int, paragraph_index: int, text: str
) -> int:
    return _execute(
        "INSERT INTO document_block "
        "(contract_id, file_id, order_index, block_type, paragraph_index, locator_type, text, raw_text, "
        " char_start_in_block, char_end_in_block, char_start_global, char_end_global, created_at, updated_at) "
        "VALUES (%s, %s, %s, 'PARAGRAPH', %s, 'PARAGRAPH', %s, %s, 0, %s, %s, %s, NOW(3), NOW(3))",
        (
            contract_id,
            file_id,
            order_index,
            paragraph_index,
            text,
            text,
            len(text),
            order_index * 30,
            order_index * 30 + len(text),
        ),
    )


def _insert_task(contract_id: int, file_id: int, *, stage: str) -> int:
    return _execute(
        "INSERT INTO review_task "
        "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
        " max_retry, idempotency_key, created_at, updated_at) "
        "VALUES (%s, %s, 'pending', %s, 0, 0, 0, 0, 3, %s, NOW(3), NOW(3))",
        (contract_id, file_id, stage, uuid.uuid4().hex + uuid.uuid4().hex),
    )


def _insert_clause(
    contract_id: int,
    task_id: int,
    *,
    clause_no: str | None,
    clause_type: str,
    title: str | None,
    start_block_id: int | None,
    end_block_id: int | None,
) -> int:
    return _execute(
        "INSERT INTO clause "
        "(contract_id, task_id, clause_no, clause_type, title, start_block_id, end_block_id, "
        " char_start_global, char_end_global, text, extract_method, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 10, '条款全文', 'RULE', NOW(3), NOW(3))",
        (contract_id, task_id, clause_no, clause_type, title, start_block_id, end_block_id),
    )


def _insert_metadata(contract_id: int, task_id: int, *, field_key: str, source_block_id: int | None) -> int:
    return _execute(
        "INSERT INTO contract_metadata "
        "(contract_id, task_id, field_key, field_label, field_value, value_type, source_block_id, "
        " extract_method, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, '乙方公司', 'TEXT', %s, 'REGEX', NOW(3), NOW(3))",
        (contract_id, task_id, field_key, f"{field_key} 展示名", source_block_id),
    )


def _insert_risk(
    task_id: int, contract_id: int, *, clause_id: int | None, risk_code: str, paragraph_index: int
) -> int:
    return _execute(
        "INSERT INTO risk_item "
        "(task_id, contract_id, clause_id, risk_code, risk_title, dimension, risk_level, source, "
        " reason, legal_basis, original_text, paragraph_index, locator_type, review_status, "
        " created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, '风险标题', '知识产权', 'HIGH', 'RULE', '成因', '依据', "
        "        '命中片段', %s, 'PARAGRAPH', 'PENDING', NOW(3), NOW(3))",
        (task_id, contract_id, clause_id, risk_code, paragraph_index),
    )


@pytest.fixture
def scene() -> Scenario:
    return _build_scenario()


def _get(client: TestClient, task_id: int) -> dict:
    response = client.get(f"/api/v1/review-tasks/{task_id}/workbench")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 1：task 不存在 → 404
# --------------------------------------------------------------------------- #
def test_a_missing_task_is_a_404(client: TestClient) -> None:
    response = client.get("/api/v1/review-tasks/99999999/workbench")

    assert response.status_code == 404
    assert response.json()["code"] == "TASK_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 2：task / contract / file
# --------------------------------------------------------------------------- #
def test_the_task_contract_and_file_are_returned(client: TestClient, scene: Scenario) -> None:
    body = _get(client, scene.task_id)

    assert body["task"]["task_id"] == scene.task_id
    assert body["task"]["current_stage"] == "CLAUSED"
    assert body["contract"]["contract_id"] == scene.contract_id
    assert body["contract"]["title"] == "工作台测试合同"
    assert body["contract"]["our_party"] == "某某科技"
    assert body["contract"]["amount"] == "1234.50", "Decimal 以字符串序列化"
    assert body["contract"]["sign_date"] == "2026-09-15"
    assert body["file"]["file_id"] == scene.docx_file_id, "取的是**本任务的**附件"
    assert body["file"]["file_name"] == "contract.docx"
    assert body["file"]["file_type"] == "DOCX", "由 file_ext 派生（与上传接口同一口径）"
    assert body["file"]["parse_status"] == "PARSED"


def test_the_task_carries_the_p10_null_fields_as_null(client: TestClient, scene: Scenario) -> None:
    """P10 的状态语义原样透出：只推进阶段，不动状态机，也不生成评分结论。"""
    task = _get(client, scene.task_id)["task"]

    assert task["status"] == "pending"
    assert task["finished_at"] is None
    assert task["risk_level_final"] is None
    assert task["conclusion"] is None


def test_a_normal_task_carries_no_block_reason(client: TestClient, scene: Scenario) -> None:
    """没被阻塞的任务：两个字段**在响应里**、值为 null（不是缺键）。

    ⚠️ "键存在且为 null"与"没有这个键"对前端是两回事：后者会让
    ``task.block_reason_code`` 变成 undefined，与"服务端明确说没有原因"混在一起。
    """
    task = _get(client, scene.task_id)["task"]

    assert "block_reason_code" in task
    assert "block_reason_msg" in task
    assert task["block_reason_code"] is None
    assert task["block_reason_msg"] is None


def test_the_query_count_stays_five(client: TestClient, scene: Scenario) -> None:
    """**加字段不加查询**：无论响应里多出几列，这条路径始终是 5 条 SELECT。

    ⚠️ 这条断言针对的是本模块 docstring 里写死的那条设计约束（不是"越少越好"的
    泛泛之谈）：工作台一次要拼 7 份数据，一旦有人图省事在循环里补查询，
    条数就会随数据量增长 —— 那种回归在功能测试里**完全看不出来**。

    P14-5-2 的 ``block_reason_*`` 就是这条约束的实例：它们来自第 1 条查询已经
    取回的 task 行，不应该、也没有多出一条查询。
    """
    from app.db.session import get_engine

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement.lstrip().split("\n")[0])

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        _get(client, scene.task_id)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)

    selects = [s for s in statements if s.upper().startswith("SELECT")]
    assert len(selects) == 5, f"应当是 5 条 SELECT，实际 {len(selects)} 条：{selects}"


def test_a_blocked_task_carries_the_real_reason(client: TestClient, scene: Scenario) -> None:
    """被阻塞的任务带着**真实**原因 —— 这正是前端展示"为什么被阻塞"的唯一来源。

    走的是真实的写入路径（``POST .../block``，P14-4），不是直接改库：
    "上报的原因"与"读出来的原因"必须是对同一份数据的两种看法。
    """
    response = client.post(
        f"/api/v1/review-tasks/{scene.task_id}/block",
        json={"block_reason_code": "UNSUPPORTED_FORMAT", "block_reason_msg": "该文件类型暂不支持"},
    )
    assert response.status_code == 200, response.text

    task = _get(client, scene.task_id)["task"]

    assert task["status"] == "blocked"
    assert task["block_reason_code"] == "UNSUPPORTED_FORMAT"
    assert task["block_reason_msg"] == "该文件类型暂不支持"
    # 阻塞不改阶段（P14-4 冻结）：任务仍停在它当时走到的位置
    assert task["current_stage"] == "CLAUSED"


def test_the_block_reason_is_scoped_to_its_own_task(client: TestClient, scene: Scenario) -> None:
    """阻塞一个任务，**另一个任务**的工作台不受影响（task 级隔离）。"""
    client.post(
        f"/api/v1/review-tasks/{scene.task_id}/block",
        json={"block_reason_code": "FILE_CORRUPTED", "block_reason_msg": "文件损坏"},
    )

    other = _get(client, scene.other_task_id)["task"]

    assert other["status"] == "pending"
    assert other["block_reason_code"] is None
    assert other["block_reason_msg"] is None


# --------------------------------------------------------------------------- #
# 3 / 4：blocks
# --------------------------------------------------------------------------- #
def test_blocks_are_ordered_by_order_index(client: TestClient, scene: Scenario) -> None:
    blocks = _get(client, scene.task_id)["blocks"]

    assert [block["order_index"] for block in blocks] == [0, 1, 2]
    assert [block["paragraph_index"] for block in blocks] == [0, 1, 2]
    assert blocks[1]["text"] == "本项目产生的知识产权归乙方所有。"
    assert blocks[0]["char_start_global"] == 0


def test_blocks_are_scoped_by_the_file_not_the_contract(client: TestClient, scene: Scenario) -> None:
    """**本模块最重要的一条。**

    同一合同下还有一份 PDF，它的 ``paragraph_index`` 也从 0 开始。若按
    ``contract_id`` 查块，这份 PDF 的段落会混进来，而风险（``paragraph_index=1``）
    会高亮到错误的那一段 —— **且不会有任何报错**。
    """
    blocks = _get(client, scene.task_id)["blocks"]

    assert scene.pdf_block_id not in [block["block_id"] for block in blocks]
    assert all(block["text"] != "另一份 PDF 的内容" for block in blocks)
    assert len(blocks) == len(scene.docx_block_ids), "只应当有本附件的那几块"


# --------------------------------------------------------------------------- #
# 5 / 6：clauses
# --------------------------------------------------------------------------- #
def test_clauses_are_returned_in_id_order(client: TestClient, scene: Scenario) -> None:
    clauses = _get(client, scene.task_id)["clauses"]

    assert [clause["clause_no"] for clause in clauses] == ["第一条", None]
    assert clauses[0]["clause_type"] == "IP"
    assert clauses[0]["title"] == "知识产权"


def test_clause_block_ids_are_translated_to_paragraph_indexes(client: TestClient, scene: Scenario) -> None:
    """``start_block_id`` / ``end_block_id`` → 段落号。

    这个换算在 Backend 做，前端**不实现**它 —— 那是一条数据库结构知识，
    抄到前端就会两边漂移且不报错。
    """
    (first, second) = _get(client, scene.task_id)["clauses"]

    assert first["start_block_id"] == scene.docx_block_ids[0]
    assert first["end_block_id"] == scene.docx_block_ids[1]
    assert first["start_paragraph_index"] == 0, "块 0 → 段落 0"
    assert first["end_paragraph_index"] == 1, "块 1 → 段落 1"

    # 第二个条款刻意没有块引用（表里 nullable）—— 换算不出来就是 null，**不伪造**
    assert (second["start_block_id"], second["end_block_id"]) == (None, None)
    assert second["start_paragraph_index"] is None
    assert second["end_paragraph_index"] is None


# --------------------------------------------------------------------------- #
# 7 / 8：metadata
# --------------------------------------------------------------------------- #
def test_metadata_is_returned_with_a_translated_source_paragraph(client: TestClient, scene: Scenario) -> None:
    metadata = _get(client, scene.task_id)["metadata"]

    assert [item["field_key"] for item in metadata] == ["counterparty_name", "contract_amount"]
    assert metadata[0]["source_block_id"] == scene.docx_block_ids[1]
    assert metadata[0]["source_paragraph_index"] == 1
    assert metadata[0]["extract_method"] == "REGEX"
    # 没有来源块 → null，不伪造
    assert metadata[1]["source_block_id"] is None
    assert metadata[1]["source_paragraph_index"] is None


def test_metadata_carries_no_quote_field(client: TestClient, scene: Scenario) -> None:
    """``contract_metadata`` 表里**没有 quote 列**（P10 已确认），DTO 也不该有。"""
    (item,) = _get(client, scene.task_id)["metadata"][:1]

    assert "quote" not in item


# --------------------------------------------------------------------------- #
# 9 / 10 / 11：risks
# --------------------------------------------------------------------------- #
def test_risks_are_returned_with_their_location(client: TestClient, scene: Scenario) -> None:
    (risk,) = _get(client, scene.task_id)["risks"]

    assert risk["risk_code"] == "IP_OWNER_SUPPLIER_001"
    assert risk["risk_level"] == "HIGH"
    assert risk["source"] == "RULE"
    assert risk["paragraph_index"] == 1
    assert risk["clause_id"] == scene.clause_id
    assert risk["locator_type"] == "PARAGRAPH"
    assert risk["review_status"] == "PENDING"


def test_risks_are_scoped_by_task(client: TestClient, scene: Scenario) -> None:
    """另一个任务的风险（同一个合同、同一个文件）不该出现。"""
    risks = _get(client, scene.task_id)["risks"]

    assert [risk["risk_code"] for risk in risks] == ["IP_OWNER_SUPPLIER_001"]
    assert all(risk["risk_id"] != scene.other_clause_id for risk in risks)


def test_a_risk_clause_id_always_points_into_this_task(client: TestClient, scene: Scenario) -> None:
    """**clause_id 不许跨任务错配。**

    返回的每个非空 ``clause_id`` 都必须出现在**本响应的** ``clauses`` 里 ——
    前端拿它去那个数组里找，找不到就会显示成"无归属条款"。
    """
    body = _get(client, scene.task_id)
    own_clause_ids = {clause["clause_id"] for clause in body["clauses"]}

    assert scene.other_clause_id not in own_clause_ids, "另一个任务的条款不该在列表里"
    for risk in body["risks"]:
        if risk["clause_id"] is not None:
            assert risk["clause_id"] in own_clause_ids


def test_the_risk_points_at_a_real_block(client: TestClient, scene: Scenario) -> None:
    """定位链的最后一环：``paragraph_index`` 必须能在 ``blocks`` 里找到。"""
    body = _get(client, scene.task_id)
    indexes = {block["paragraph_index"] for block in body["blocks"]}

    for risk in body["risks"]:
        assert risk["paragraph_index"] in indexes, "风险指向的段落必须真的在原文里"


# --------------------------------------------------------------------------- #
# 11b：人工复核字段（P13-2）
# --------------------------------------------------------------------------- #
#: 复核相关的四列。工作台必须**全部**返回 —— 前端要靠它们把"AI 判断"与
#: "法务判断"分开呈现（P13-3 的复核 UI 依赖这个契约）。
REVIEW_FIELDS = ("review_status", "reviewer_id", "review_comment", "reviewed_at")


def test_the_risk_dto_exposes_all_four_review_fields(client: TestClient, scene: Scenario) -> None:
    for risk in _get(client, scene.task_id)["risks"]:
        assert set(REVIEW_FIELDS) <= set(risk), f"缺少复核字段：{set(REVIEW_FIELDS) - set(risk)}"


def test_an_unreviewed_risk_returns_nulls_for_the_review_details(
    client: TestClient, scene: Scenario
) -> None:
    """AI 刚产出的风险：状态是 ``PENDING``，其余三列都是 ``null``。

    这三个 null 是**事实**（还没人复核过），不是"取不到值" —— 前端据此区分
    "未复核"与"复核了但没写意见"。
    """
    (risk,) = _get(client, scene.task_id)["risks"]

    assert risk["review_status"] == "PENDING"
    assert risk["reviewer_id"] is None
    assert risk["review_comment"] is None
    assert risk["reviewed_at"] is None


def test_the_review_fields_do_not_disturb_the_ai_facts(client: TestClient, scene: Scenario) -> None:
    """新增四列不能把原有的 AI 字段挤掉或改名 —— 前端靠它们渲染风险卡片。"""
    (risk,) = _get(client, scene.task_id)["risks"]

    assert risk["risk_title"] == "风险标题"
    assert risk["dimension"] == "知识产权"
    assert risk["risk_level"] == "HIGH"
    assert risk["source"] == "RULE"
    assert risk["reason"] == "成因"
    assert risk["legal_basis"] == "依据"
    assert risk["original_text"] == "命中片段"
    assert risk["paragraph_index"] == 1
    assert risk["clause_id"] == scene.clause_id


# --------------------------------------------------------------------------- #
# 12：空集合
# --------------------------------------------------------------------------- #
def test_empty_collections_are_two_hundred_with_empty_arrays(client: TestClient) -> None:
    """``blocks`` / ``clauses`` / ``metadata`` / ``risks`` 全为空也是 200 —— 这是正常结论。"""
    contract_id = _execute(
        "INSERT INTO contract (contract_no, title, contract_type, source, status, created_at, updated_at) "
        "VALUES (%s, '空任务合同', 'PURCHASE', 'UPLOAD', 'PENDING', NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:12]}",),
    )
    file_id = _insert_file(contract_id, "empty.docx", ".docx", "PENDING")
    task_id = _insert_task(contract_id, file_id, stage="UPLOADED")

    body = _get(client, task_id)

    assert body["blocks"] == []
    assert body["clauses"] == []
    assert body["metadata"] == []
    assert body["risks"] == []


# --------------------------------------------------------------------------- #
# 13：progress 复用 P11-3
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stage", ["UPLOADED", "PARSED", "CLAUSED", "REVIEWED"])
def test_progress_reuses_the_p11_3_mapping(client: TestClient, stage: str) -> None:
    """**不复制一份映射** —— 直接断言它与 ``contract_query.progress_for_stage`` 同值。

    这条用例是防"两套口径"的：哪天 P11-3 改了映射而 workbench 忘了跟，
    这里会立刻红。
    """
    contract_id = _execute(
        "INSERT INTO contract (contract_no, title, contract_type, source, status, created_at, updated_at) "
        "VALUES (%s, '进度合同', 'PURCHASE', 'UPLOAD', 'PENDING', NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:12]}",),
    )
    file_id = _insert_file(contract_id, "p.docx", ".docx", "PENDING")
    task_id = _insert_task(contract_id, file_id, stage=stage)

    body = _get(client, task_id)

    assert body["task"]["progress"] == progress_for_stage(stage)


# --------------------------------------------------------------------------- #
# 14：不泄露 ORM 内部字段
# --------------------------------------------------------------------------- #
def test_the_response_does_not_leak_internal_columns(client: TestClient, scene: Scenario) -> None:
    body = _get(client, scene.task_id)
    serialized = str(body)

    for internal in (
        "storage_path",  # 附件：存储后端的内部标识
        "idempotency_key",  # 任务：幂等键（含 prompt_version，属内部编排）
        "worker_id",
        "heartbeat_at",
        "next_retry_at",
        # ⚠️ ``block_reason_code`` / ``block_reason_msg`` **曾经**在这张清单里
        # （P11 时它们是队列/阻塞的内部字段）。P14-5-2 把它们正式纳入工作台契约：
        # 前端必须能显示"为什么被阻塞"，而不是从 ``status`` 猜。因此它们从
        # "内部字段"挪到了 DTO —— 这里也就不能再断言它们不出现。
        "current_task_id",  # 合同：过期冗余字段
        "approval_instance_id",
        "applicant_id",
        "raw_text",  # 块：审计用原文（DTO 只给归一化文本）
        "ocr_confidence",
        "bbox_json",
    ):
        assert internal not in serialized, f"响应里不该出现内部字段 {internal}"


def test_the_response_has_exactly_the_declared_top_level_keys(client: TestClient, scene: Scenario) -> None:
    assert set(_get(client, scene.task_id)) == {
        "task",
        "contract",
        "file",
        "blocks",
        "clauses",
        "metadata",
        "risks",
    }


# --------------------------------------------------------------------------- #
# 15：跨 task / 跨 file 隔离（全场景交叉核对）
# --------------------------------------------------------------------------- #
def test_two_tasks_on_the_same_file_share_blocks_but_not_conclusions(
    client: TestClient, scene: Scenario
) -> None:
    """同一个文件的两个任务：**原文相同、结论各自独立**。

    这是 file 级 / task 级归属规则最直观的一次验证 ——
    块（文件级）两边一模一样，条款 / 元数据 / 风险（任务级）完全不相交。
    """
    first = _get(client, scene.task_id)
    second = _get(client, scene.other_task_id)

    assert first["file"]["file_id"] == second["file"]["file_id"]
    assert [b["block_id"] for b in first["blocks"]] == [b["block_id"] for b in second["blocks"]]

    first_clauses = {c["clause_id"] for c in first["clauses"]}
    second_clauses = {c["clause_id"] for c in second["clauses"]}
    assert first_clauses & second_clauses == set(), "条款必须互不相交"

    first_meta = {m["field_key"] for m in first["metadata"]}
    second_meta = {m["field_key"] for m in second["metadata"]}
    assert first_meta & second_meta == set()

    first_risks = {r["risk_code"] for r in first["risks"]}
    second_risks = {r["risk_code"] for r in second["risks"]}
    assert first_risks & second_risks == set()
    assert first_risks == {"IP_OWNER_SUPPLIER_001"}
    assert second_risks == {"LIAB_UNLIMITED_001"}


def test_a_task_on_the_other_file_gets_that_files_blocks(client: TestClient, scene: Scenario) -> None:
    """**按文件隔离的正面证明**：另一个文件的任务看到的是它自己那份原文。"""
    other_file_task = _insert_task(scene.contract_id, scene.pdf_file_id, stage="UPLOADED")

    blocks = _get(client, other_file_task)["blocks"]

    assert len(blocks) == 1
    assert blocks[0]["block_id"] == scene.pdf_block_id
    assert blocks[0]["text"] == "另一份 PDF 的内容"
    assert blocks[0]["paragraph_index"] == 0, "两份文件的段落号各自从 0 开始"
