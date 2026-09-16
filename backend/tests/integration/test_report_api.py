"""``GET /api/v1/review-tasks/{task_id}/report/export`` 的集成测试（**需要真实 MySQL**）。

这里断言的是**只有真实数据库 + 真实 ASGI 才能验**的三件事：

1. **隔离** —— 同一个合同下的另一次审查，它的风险 / 条款 / 元数据一个字都不能
   出现在本次报告里。P12-2 的单元测试只覆盖了装配逻辑，隔离是 SQL 的 WHERE
   说了算，只能在这里验
2. **门禁** —— 未完成的任务必须是 409 而不是一份"零风险"的报告
3. **坏数据的分辨** —— 任务在、合同/附件没了，要报对应的 404 而不是 500 或 404 混用

隔离方式：自造 ``RPT-`` 前缀的合同 + ``RPT-TASK-`` 前缀的幂等键，收尾按外键反序删除。
坏数据（父行不存在）走 :func:`_execute_without_fk_checks`，见那里的说明。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import unquote

import pymysql
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.base import UTC_SESSION_INIT_COMMAND
from app.main import app
from app.utils.datetime_utils import utcnow

PREFIX = "RPT-"
TASK_KEY_PREFIX = "RPT-TASK-"


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
        # ⚠️ P12-4 修正：夹具的裸连接此前**绕过了**项目的会话时区约定。
        #
        # ``app.db.base.build_connect_args()`` 的 docstring 写着"所有创建 MySQL 连接的
        # 地方都必须经过这里，否则就会绕开 UTC 会话时区约定" —— 而这里的 pymysql
        # 直连没有 ``init_command``，于是夹具里的 ``NOW(3)`` 返回的是**会话时区**
        # （中文环境 = UTC+8），却写进了应用按 UTC 解释的列。
        #
        # 实测后果：同一条记录，应用写出来是 08:43，夹具用 ``NOW(3)`` 写出来是 16:43，
        # 而报告照 UTC 渲染成 "16:43 (UTC)" —— 时间列比真实时刻**晚 8 小时**。
        # 生产路径没有这个问题（引擎自带 init_command 且写库走 ``utcnow()``）；
        # 这是**夹具保真度**问题，靠这里补上同一条 init_command 对齐。
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


def _execute_without_fk_checks(sql: str, params: tuple = ()) -> int:
    """在同一条连接上临时关掉外键检查后写入。

    用来造"父行不在"的坏数据：``review_task`` 上的两个 FK 都是 NOT NULL +
    RESTRICT，正常途径根本造不出"任务在、合同没了"。而那两条分支
    （``CONTRACT_NOT_FOUND`` / ``FILE_NOT_FOUND``）**只有真的把数据弄坏**才测得到，
    否则永远是一段没人跑过的代码。
    """
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            try:
                cursor.execute(sql, params)
                return cursor.lastrowid or cursor.rowcount
            finally:
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
    finally:
        conn.close()


def _purge() -> None:
    # 任务按**幂等键前缀**找：坏数据的 contract_id 指向不存在的合同，
    # 按合同反查是找不到它们的（那正是被测的状态）
    task_ids = [
        row[0]
        for row in _query(
            "SELECT id FROM review_task WHERE idempotency_key LIKE %s", (f"{TASK_KEY_PREFIX}%",)
        )
    ]
    if task_ids:
        task_ph = ",".join(["%s"] * len(task_ids))
        for table in ("risk_item", "contract_metadata", "clause"):
            _query(f"DELETE FROM {table} WHERE task_id IN ({task_ph})", tuple(task_ids))
        _query(f"DELETE FROM review_task WHERE id IN ({task_ph})", tuple(task_ids))

    contract_ids = [
        row[0] for row in _query("SELECT id FROM contract WHERE contract_no LIKE %s", (f"{PREFIX}%",))
    ]
    if contract_ids:
        placeholders = ",".join(["%s"] * len(contract_ids))
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
    """一个"带干扰"的场景：同一个合同、**同一个附件**下跑**两次审查**。

    ::

        contract ─┬─ contract_file ─┬─ review_task A（本场景的主角）
                  │                 └─ review_task B（干扰源）
                  └─ document_block ×N（**文件级**：两个任务共享同一批块）

    ``document_block`` 只有 ``file_id``、没有 ``task_id`` —— 两个任务看到的是**同一批
    段落**；而 ``clause`` / ``contract_metadata`` / ``risk_item`` 是**任务级**的。
    报告必须严格按 ``task_id`` 隔离，同时不能因为"块是共享的"就把条款也算共享。
    """

    contract_id: int
    contract_no: str
    file_id: int
    task_id: int
    other_task_id: int
    clause_id: int
    block_ids: tuple[int, ...]


def _build_scenario(
    *, stage: str = "REVIEWED", contract_no: str = "RPT-2026-001", with_risks: bool = True
) -> Scenario:
    # 加随机后缀是为了 `contract_no` 的 UNIQUE 约束在多次运行之间不打架；
    # 断言文件名时必须用**这个实际值**，不能拿传进来的前缀去比
    full_no = f"{contract_no}-{uuid.uuid4().hex[:10]}"
    contract_id = _execute(
        "INSERT INTO contract "
        "(contract_no, title, contract_type, source, status, our_party, counterparty, amount, currency, "
        " sign_date, dept, created_at, updated_at) "
        "VALUES (%s, '报告测试合同', 'PURCHASE', 'UPLOAD', 'PENDING', '某某科技', '乙方公司', "
        "        1234.50, 'CNY', '2026-09-15', '法务部', NOW(3), NOW(3))",
        (full_no,),
    )
    file_id = _insert_file(contract_id, "采购合同-风险版.docx", ".docx")
    task_id = _insert_task(contract_id, file_id, stage=stage)
    other_task_id = _insert_task(contract_id, file_id, stage="REVIEWED")

    # 块是**文件级**的：两个任务共享同一批（这正是要防的"看起来共享、实则不该共享"）
    block_ids = tuple(
        _insert_block(contract_id, file_id, order_index=i, paragraph_index=i, text=text)
        for i, text in enumerate(
            ("第三条 知识产权", "本项目产生的知识产权归乙方所有。", "第四条 付款条件")
        )
    )

    clause_id = _insert_clause(
        contract_id,
        task_id,
        clause_no="第三条",
        title="知识产权",
        start_block_id=block_ids[0],
        end_block_id=block_ids[1],
    )
    other_clause_id = _insert_clause(
        contract_id, other_task_id, clause_no="另一条", title="别处的条款"
    )

    # 本任务：三个等级各一条 + 一条没有条款 + 一条指向别的任务的条款
    if with_risks:
        _insert_risk(
            task_id, contract_id, clause_id=clause_id, risk_level="HIGH", title="知识产权归属相对方"
        )
        _insert_risk(task_id, contract_id, clause_id=clause_id, risk_level="MEDIUM", title="付款条件不利")
        _insert_risk(task_id, contract_id, clause_id=None, risk_level="LOW", title="缺少争议解决条款")
        _insert_risk(task_id, contract_id, clause_id=other_clause_id, risk_level="LOW", title="跨任务条款")
        # 另一次审查的风险：绝不能出现在本次报告里
        _insert_risk(
            other_task_id, contract_id, clause_id=other_clause_id, risk_level="HIGH", title="别的任务的风险"
        )

    _insert_metadata(contract_id, task_id, field_key="counterparty_name", label="相对方名称")
    _insert_metadata(contract_id, other_task_id, field_key="other_task_field", label="别的任务的字段")

    return Scenario(
        contract_id=contract_id,
        contract_no=full_no,
        file_id=file_id,
        task_id=task_id,
        other_task_id=other_task_id,
        clause_id=clause_id,
        block_ids=block_ids,
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


def _insert_file(contract_id: int, name: str, ext: str) -> int:
    return _execute(
        "INSERT INTO contract_file "
        "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
        " created_at, updated_at) "
        "VALUES (%s, %s, %s, 1024, %s, %s, 0, 'PARSED', NOW(3), NOW(3))",
        (contract_id, name, ext, uuid.uuid4().hex + uuid.uuid4().hex, f"{contract_id}/{name}"),
    )


def _insert_task(contract_id: int, file_id: int, *, stage: str) -> int:
    return _execute(
        "INSERT INTO review_task "
        "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
        " max_retry, idempotency_key, created_at, updated_at) "
        "VALUES (%s, %s, 'pending', %s, 0, 0, 0, 0, 3, %s, NOW(3), NOW(3))",
        (contract_id, file_id, stage, f"{TASK_KEY_PREFIX}{uuid.uuid4().hex}"),
    )


def _insert_dangling_task(*, contract_id: int, file_id: int) -> int:
    """造一个指向**不存在的**合同或附件的任务（外键检查临时关掉）。"""
    return _execute_without_fk_checks(
        "INSERT INTO review_task "
        "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
        " max_retry, idempotency_key, created_at, updated_at) "
        "VALUES (%s, %s, 'pending', 'REVIEWED', 0, 0, 0, 0, 3, %s, NOW(3), NOW(3))",
        (contract_id, file_id, f"{TASK_KEY_PREFIX}{uuid.uuid4().hex}"),
    )


def _insert_clause(
    contract_id: int,
    task_id: int,
    *,
    clause_no: str,
    title: str,
    start_block_id: int | None = None,
    end_block_id: int | None = None,
) -> int:
    return _execute(
        "INSERT INTO clause "
        "(contract_id, task_id, clause_no, clause_type, title, start_block_id, end_block_id, "
        " char_start_global, char_end_global, text, extract_method, created_at, updated_at) "
        "VALUES (%s, %s, %s, 'IP', %s, %s, %s, 0, 10, %s, 'RULE', NOW(3), NOW(3))",
        (
            contract_id,
            task_id,
            clause_no,
            title,
            start_block_id,
            end_block_id,
            # 条款正文带上自己的标识，报告附录里就能断言"出现的是哪一条"
            f"{clause_no} {title} 的条款正文",
        ),
    )


def _insert_metadata(contract_id: int, task_id: int, *, field_key: str, label: str) -> int:
    return _execute(
        "INSERT INTO contract_metadata "
        "(contract_id, task_id, field_key, field_label, field_value, value_type, source_block_id, "
        " extract_method, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, 'TEXT', NULL, 'REGEX', NOW(3), NOW(3))",
        (contract_id, task_id, field_key, label, f"{field_key} 的值"),
    )


def _insert_risk(
    task_id: int, contract_id: int, *, clause_id: int | None, risk_level: str, title: str
) -> int:
    return _execute(
        "INSERT INTO risk_item "
        "(task_id, contract_id, clause_id, risk_code, risk_title, dimension, risk_level, source, "
        " reason, legal_basis, original_text, paragraph_index, locator_type, review_status, "
        " created_at, updated_at) "
        "VALUES (%s, %s, %s, 'IP_OWNER_SUPPLIER_001', %s, '知识产权', %s, 'RULE', '成因', '依据', "
        "        '命中片段', 23, 'PARAGRAPH', 'PENDING', NOW(3), NOW(3))",
        (task_id, contract_id, clause_id, title, risk_level),
    )


@pytest.fixture
def scene() -> Scenario:
    return _build_scenario()


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _export(client: TestClient, task_id: int):
    return client.get(f"/api/v1/review-tasks/{task_id}/report/export")


def _disposition(response) -> str:
    return response.headers["content-disposition"]


def _decoded_filename(response) -> str:
    """从 ``filename*=UTF-8''…`` 解出客户端实际会用的文件名。"""
    return unquote(_disposition(response).split("filename*=UTF-8''", 1)[1])


# --------------------------------------------------------------------------- #
# 1：成功导出
# --------------------------------------------------------------------------- #
def test_a_reviewed_task_exports_a_markdown_report(client: TestClient, scene: Scenario) -> None:
    response = _export(client, scene.task_id)

    assert response.status_code == 200, response.text
    assert response.text.startswith("# 合同审查报告\n")


def test_the_response_is_the_final_markdown(client: TestClient, scene: Scenario) -> None:
    body = _export(client, scene.task_id).text

    for heading in (
        "## 一、合同基本信息",
        "## 二、审查任务信息",
        "## 三、风险概览",
        "## 四、风险明细",
        "## 五、条款附录",
        "## 六、合同信息提取结果",
    ):
        assert heading in body, f"缺少章节：{heading}"

    # 库里的事实确实被渲染进去了
    assert "报告测试合同" in body
    assert "知识产权归属相对方" in body
    assert "相对方名称" in body
    assert "第三条 知识产权" in body
    # 概览是**事实统计**，不是结论
    assert "本次审查共发现 4 项风险" in body


def test_the_content_type_is_markdown_with_utf8(client: TestClient, scene: Scenario) -> None:
    content_type = _export(client, scene.task_id).headers["content-type"]

    assert content_type.startswith("text/markdown")
    assert "charset=utf-8" in content_type


# --------------------------------------------------------------------------- #
# 2：中文文件名
# --------------------------------------------------------------------------- #
def test_the_filename_is_a_chinese_attachment_name(client: TestClient, scene: Scenario) -> None:
    response = _export(client, scene.task_id)
    disposition = _disposition(response)

    assert disposition.startswith("attachment;")
    assert _decoded_filename(response) == f"{scene.contract_no}-审查报告-{scene.task_id}.md"


def test_the_header_is_latin1_encodable_over_real_http(client: TestClient, scene: Scenario) -> None:
    """真实 HTTP 栈走一遍：中文若裸写在 ``filename="…"``，这里就会炸或被改写。"""
    disposition = _disposition(_export(client, scene.task_id))

    assert disposition.encode("latin-1")
    assert "审查报告" not in disposition


def test_the_ascii_fallback_is_present_for_old_clients(client: TestClient, scene: Scenario) -> None:
    disposition = _disposition(_export(client, scene.task_id))
    fallback = disposition.split('filename="', 1)[1].split('"', 1)[0]

    assert fallback.isascii()
    assert fallback.endswith(".md")


# --------------------------------------------------------------------------- #
# 3：隔离 —— 另一次审查的数据不能渗进来
# --------------------------------------------------------------------------- #
def test_another_task_on_the_same_contract_is_invisible(
    client: TestClient, scene: Scenario
) -> None:
    """**本文件最重要的一条。**

    同一个合同可以跑多次审查，同一个附件也会被复用。按 ``contract_id`` 或
    ``file_id`` 查会让另一次审查的风险/条款/元数据混进报告 —— 而且**不会报错**。
    """
    body = _export(client, scene.task_id).text

    assert "别的任务的风险" not in body
    assert "别处的条款" not in body
    assert "别的任务的字段" not in body
    assert "本次审查共发现 4 项风险" in body  # 只数本任务那 4 条


def test_the_other_task_gets_its_own_report(client: TestClient, scene: Scenario) -> None:
    body = _export(client, scene.other_task_id).text

    assert "别的任务的风险" in body
    assert "知识产权归属相对方" not in body


# --------------------------------------------------------------------------- #
# 4：门禁
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stage", ["UPLOADED", "PARSED", "CLAUSED", "SOMETHING_NEW"])
def test_an_unfinished_task_is_a_409(client: TestClient, stage: str) -> None:
    """``CLAUSED`` 尤其关键：风险还没落库，此时导出会得到一份**零风险**的报告。"""
    scenario = _build_scenario(stage=stage, contract_no="RPT-UNFINISHED")

    response = _export(client, scenario.task_id)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "REPORT_NOT_READY"


def test_the_gate_is_not_a_404(client: TestClient) -> None:
    """任务**存在**，只是还不能出报告 —— 报 404 会让前端显示成"任务不存在"。"""
    scenario = _build_scenario(stage="UPLOADED", contract_no="RPT-GATE")

    assert _export(client, scenario.task_id).status_code != 404


# --------------------------------------------------------------------------- #
# 5：404 的三种
# --------------------------------------------------------------------------- #
def test_a_missing_task_is_a_404(client: TestClient) -> None:
    response = _export(client, 99999999)

    assert response.status_code == 404
    assert response.json()["code"] == "TASK_NOT_FOUND"


def test_a_task_with_a_missing_contract_is_a_404(client: TestClient, scene: Scenario) -> None:
    """数据被外力破坏时**不给半份报告**，而是明确报错。"""
    task_id = _insert_dangling_task(contract_id=99999999, file_id=scene.file_id)

    response = _export(client, task_id)

    assert response.status_code == 404
    assert response.json()["code"] == "CONTRACT_NOT_FOUND"


def test_a_task_with_a_missing_file_is_a_404(client: TestClient, scene: Scenario) -> None:
    task_id = _insert_dangling_task(contract_id=scene.contract_id, file_id=99999999)

    response = _export(client, task_id)

    assert response.status_code == 404
    assert response.json()["code"] == "FILE_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 6：重复下载
# --------------------------------------------------------------------------- #
def test_repeated_downloads_are_allowed_and_identical(client: TestClient, scene: Scenario) -> None:
    """没有副作用、没有报告记录、没有 report_id —— 重复下载天然幂等且字节相同。

    逐字节相同还顺带证明了渲染是**确定性**的（报告里没有"生成时间"之类的字段）。
    """
    first = _export(client, scene.task_id)
    second = _export(client, scene.task_id)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.text == second.text


def test_exporting_does_not_create_any_row(client: TestClient, scene: Scenario) -> None:
    """导出是纯读 —— 跑一次不该往库里写任何东西。"""
    before = _query("SELECT COUNT(*) FROM risk_item WHERE task_id = %s", (scene.task_id,))[0][0]

    _export(client, scene.task_id)

    after = _query("SELECT COUNT(*) FROM risk_item WHERE task_id = %s", (scene.task_id,))[0][0]
    assert before == after


def test_exporting_does_not_advance_the_task(client: TestClient, scene: Scenario) -> None:
    before = _query("SELECT current_stage, status FROM review_task WHERE id = %s", (scene.task_id,))[0]

    _export(client, scene.task_id)

    after = _query("SELECT current_stage, status FROM review_task WHERE id = %s", (scene.task_id,))[0]
    assert before == after == ("REVIEWED", "pending")


# --------------------------------------------------------------------------- #
# 7：空数据的报告仍然出得来
# --------------------------------------------------------------------------- #
def test_a_task_without_risks_still_exports(client: TestClient) -> None:
    """空风险**是结论不是错误** —— 报告照样出得来，只是写着"未发现风险"。"""
    scenario = _build_scenario(contract_no="RPT-EMPTY", with_risks=False)

    response = _export(client, scenario.task_id)

    assert response.status_code == 200
    assert "本次审查未发现风险。" in response.text


# --------------------------------------------------------------------------- #
# 8：块是文件级的，结论是任务级的
# --------------------------------------------------------------------------- #
def _workbench(client: TestClient, task_id: int) -> dict:
    response = client.get(f"/api/v1/review-tasks/{task_id}/workbench")
    assert response.status_code == 200, response.text
    return response.json()


def test_the_two_tasks_share_the_same_document_blocks(client: TestClient, scene: Scenario) -> None:
    """``document_block`` 只有 ``file_id``、没有 ``task_id`` —— 同一附件下看到的是同一批段落。"""
    first = _workbench(client, scene.task_id)
    second = _workbench(client, scene.other_task_id)

    assert [b["block_id"] for b in first["blocks"]] == [b["block_id"] for b in second["blocks"]]
    assert len(first["blocks"]) == len(scene.block_ids)


def test_sharing_blocks_does_not_share_any_task_scoped_data(
    client: TestClient, scene: Scenario
) -> None:
    """共享段落 **不等于** 共享结论。

    这是个很容易踩的坑：既然块能共享（"按文件查"），就会有人顺手把条款、元数据也
    按 ``contract_id`` 或 ``file_id`` 查。报告里于是出现另一次审查的条款与风险，
    **而且不会报错**。
    """
    first = _workbench(client, scene.task_id)
    second = _workbench(client, scene.other_task_id)

    assert {c["clause_id"] for c in first["clauses"]}.isdisjoint(
        {c["clause_id"] for c in second["clauses"]}
    )
    assert {m["field_key"] for m in first["metadata"]}.isdisjoint(
        {m["field_key"] for m in second["metadata"]}
    )
    assert {r["risk_id"] for r in first["risks"]}.isdisjoint(
        {r["risk_id"] for r in second["risks"]}
    )


def test_the_report_keeps_the_same_task_scoping_as_the_workbench(
    client: TestClient, scene: Scenario
) -> None:
    """报告侧再看一遍同一条边界 —— 两个读取实现各写各的查询，得各守各的隔离。"""
    body = _export(client, scene.task_id).text

    assert "第三条 知识产权 的条款正文" in body
    assert "另一条 别处的条款 的条款正文" not in body
    assert "counterparty_name 的值" in body
    assert "other_task_field 的值" not in body


# --------------------------------------------------------------------------- #
# 9：Workbench ↔ Report 交叉一致性（漂移监控）
# --------------------------------------------------------------------------- #
def _risk_headings(body: str) -> list[str]:
    """报告里 ``### N. [LEVEL] 标题`` 那些行，按出现顺序。"""
    return [line for line in body.splitlines() if re.match(r"^### \d+\. \[", line)]


def _positions(body: str, needles: list[str]) -> list[int]:
    return [body.index(needle) for needle in needles]


def test_the_report_and_the_workbench_agree_on_the_risk_count(
    client: TestClient, scene: Scenario
) -> None:
    """**P12-4 的核心断言**：两个读取实现说的是不是同一件事。

    ``workbench_query`` 与 ``report_query`` 是两份独立写的查询，隔离规则也各写一遍。
    它们漂移时**谁都不会报错** —— 只有交叉比对才看得出来。
    """
    workbench = _workbench(client, scene.task_id)
    body = _export(client, scene.task_id).text

    assert len(_risk_headings(body)) == len(workbench["risks"])
    assert f"本次审查共发现 {len(workbench['risks'])} 项风险" in body


def test_the_report_lists_the_workbench_risks_in_the_same_order(
    client: TestClient, scene: Scenario
) -> None:
    workbench = _workbench(client, scene.task_id)
    body = _export(client, scene.task_id).text

    titles = [risk["risk_title"] for risk in workbench["risks"]]
    positions = _positions(body, titles)
    assert positions == sorted(positions), f"风险顺序不一致：{titles}"


def test_every_workbench_risk_level_shows_up_in_the_report_overview(
    client: TestClient, scene: Scenario
) -> None:
    workbench = _workbench(client, scene.task_id)
    body = _export(client, scene.task_id).text

    for level in ("HIGH", "MEDIUM", "LOW"):
        expected = sum(1 for risk in workbench["risks"] if risk["risk_level"] == level)
        assert f"| {level} | {expected} |" in body


def test_the_report_lists_the_workbench_clauses_in_the_same_order(
    client: TestClient, scene: Scenario
) -> None:
    workbench = _workbench(client, scene.task_id)
    body = _export(client, scene.task_id).text

    labels = [" ".join(part for part in (c["clause_no"], c["title"]) if part) for c in workbench["clauses"]]
    positions = _positions(body, labels)
    assert positions == sorted(positions), f"条款顺序不一致：{labels}"


def test_the_report_lists_the_workbench_metadata_labels(
    client: TestClient, scene: Scenario
) -> None:
    workbench = _workbench(client, scene.task_id)
    body = _export(client, scene.task_id).text

    labels = [item["field_label"] for item in workbench["metadata"]]
    assert labels, "场景里应当有元数据，否则这条断言是空转"
    positions = _positions(body, labels)
    assert positions == sorted(positions), f"元数据顺序不一致：{labels}"


def test_the_report_resolves_risk_clauses_the_same_way_as_the_workbench(
    client: TestClient, scene: Scenario
) -> None:
    """同一条风险，两边认到的条款必须是同一条（或同样地"没有"）。"""
    workbench = _workbench(client, scene.task_id)
    body = _export(client, scene.task_id).text

    clauses_by_id = {clause["clause_id"]: clause for clause in workbench["clauses"]}
    for risk in workbench["risks"]:
        clause = clauses_by_id.get(risk["clause_id"]) if risk["clause_id"] else None
        if clause is None:
            # 工作台也解析不出来 → 报告应当说"未关联"或"缺失"，而不是编一个条款
            continue
        label = " ".join(part for part in (clause["clause_no"], clause["title"]) if part)
        assert label in body, f"风险 {risk['risk_id']} 的条款 {label} 没出现在报告里"


def test_the_workbench_and_the_report_agree_on_the_contract_and_file(
    client: TestClient, scene: Scenario
) -> None:
    """报告头部的合同/附件信息必须与工作台同源 —— 两份都不能自己算。"""
    workbench = _workbench(client, scene.task_id)
    body = _export(client, scene.task_id).text

    assert workbench["contract"]["contract_no"] == scene.contract_no
    assert scene.contract_no in body
    assert workbench["file"]["file_name"] in body
    assert workbench["file"]["sha256"] in body


# --------------------------------------------------------------------------- #
# 10：只读 —— 导出前后逐列比对
# --------------------------------------------------------------------------- #
def test_exporting_changes_nothing_at_all(client: TestClient, scene: Scenario) -> None:
    """**完整版无副作用断言**：任务的关键列 + 四张数据表的行数，导出前后必须一样。

    一个"顺手把任务标记成已完成"或"顺手写一条 report 记录"的实现会在这里露馅。
    """
    before = _snapshot(scene)

    # 守卫：快照必须是"有内容"的，否则前后相等只是因为两边都是空的（空转通过）
    assert before["counts"]["risk_item"] > 0
    assert before["counts"]["clause"] > 0
    assert before["counts"]["contract_metadata"] > 0
    assert before["counts"]["document_block"] > 0

    assert _export(client, scene.task_id).status_code == 200
    assert _export(client, scene.task_id).status_code == 200

    assert _snapshot(scene) == before


def _snapshot(scene: Scenario) -> dict:
    task = _query(
        "SELECT current_stage, status, finished_at, risk_level_final, conclusion "
        "FROM review_task WHERE id = %s",
        (scene.task_id,),
    )[0]
    counts = {
        table: _query(f"SELECT COUNT(*) FROM {table} WHERE task_id = %s", (scene.task_id,))[0][0]
        for table in ("risk_item", "clause", "contract_metadata")
    }
    counts["document_block"] = _query(
        "SELECT COUNT(*) FROM document_block WHERE file_id = %s", (scene.file_id,)
    )[0][0]
    return {"task": task, "counts": counts}


def test_the_export_does_not_fill_in_the_conclusion_columns(
    client: TestClient, scene: Scenario
) -> None:
    """导出**不**往 ``risk_level_final`` / ``conclusion`` 里写东西。

    那是 §11.2 的评分结果，当前库里恒为 NULL；导出一份报告不等于做出了审查结论。
    """
    _export(client, scene.task_id)

    risk_level_final, conclusion = _query(
        "SELECT risk_level_final, conclusion FROM review_task WHERE id = %s", (scene.task_id,)
    )[0]

    assert risk_level_final is None
    assert conclusion is None


def test_the_export_does_not_finish_the_task(client: TestClient, scene: Scenario) -> None:
    """任务仍然停在 ``pending`` / ``finished_at IS NULL`` —— 只推 ``current_stage`` 是既定裁决。"""
    _export(client, scene.task_id)

    status, finished_at = _query(
        "SELECT status, finished_at FROM review_task WHERE id = %s", (scene.task_id,)
    )[0]

    assert status == "pending"
    assert finished_at is None


# --------------------------------------------------------------------------- #
# 11：夹具时区（P12-4 修正的回归保护）
# --------------------------------------------------------------------------- #
def test_the_fixture_writes_utc_timestamps_like_the_application(
    client: TestClient, scene: Scenario
) -> None:
    """夹具连接必须和应用的引擎一样把会话时区钉死为 UTC。

    此前夹具用裸 ``pymysql`` 直连（没有 ``init_command``），``NOW(3)`` 写进去的是
    **本地时间**（中文环境 UTC+8），而应用按 UTC 解释同一列 —— 同一条记录能差 8 小时。
    生产路径没有这个问题（引擎自带 ``init_command``，且写库走 ``utcnow()``）。

    这里用"MYSQL 认为现在几点"直接验证会话时区：UTC 会话下它与 ``utcnow()`` 相差
    应当在一分钟以内（本地时区会差 8 小时）。
    """
    db_now = _query("SELECT NOW(3)")[0][0]
    drift = abs((db_now - utcnow()).total_seconds())

    assert drift < 60, f"夹具连接的会话时区不是 UTC（与 utcnow() 相差 {drift / 3600:.1f} 小时）"


def test_the_report_renders_the_stored_time_as_utc(client: TestClient, scene: Scenario) -> None:
    """报告按项目既有语义（naive UTC）渲染时间，且明确标注 —— 不标注会被当成本地时间。"""
    body = _export(client, scene.task_id).text

    assert "(UTC)" in body
    created_at = _query("SELECT created_at FROM review_task WHERE id = %s", (scene.task_id,))[0][0]
    assert created_at.strftime("%Y-%m-%d %H:%M") in body


# --------------------------------------------------------------------------- #
# 12：人工复核标注（P13-4）
# --------------------------------------------------------------------------- #
# 端到端：走 **P13-1 的真实 PATCH 接口**写下复核结论，再导出报告 ——
# 这样"写入侧改了哪些列"与"报告读了哪些列"是在同一条链路上被验证的，
# 而不是靠测试自己 UPDATE 一个值来假装。
def _risk_ids(task_id: int) -> list[int]:
    rows = _query("SELECT id FROM risk_item WHERE task_id = %s ORDER BY id", (task_id,))
    return [row[0] for row in rows]


def _review(client: TestClient, task_id: int, risk_id: int, **body: object):
    return client.patch(f"/api/v1/review-tasks/{task_id}/risks/{risk_id}", json=body)


def _first_risk_id(client: TestClient, scene: Scenario) -> int:
    ids = _risk_ids(scene.task_id)
    assert ids, "场景里应当有风险，否则这些断言是空转"
    return ids[0]


def test_a_report_with_unreviewed_risks_still_exports(client: TestClient, scene: Scenario) -> None:
    """**历史数据兼容**：P13 之前的风险全是 PENDING + 两个 NULL，报告必须照常生成。"""
    response = _export(client, scene.task_id)

    assert response.status_code == 200
    assert "| 人工复核 | 待复核 |" in response.text
    assert "- **复核意见**" not in response.text
    assert "- **复核时间**" not in response.text


def test_the_report_shows_a_confirmed_review(client: TestClient, scene: Scenario) -> None:
    risk_id = _first_risk_id(client, scene)
    assert (
        _review(
            client,
            scene.task_id,
            risk_id,
            review_status="CONFIRMED",
            review_comment="已与业务确认，接受该条款",
        ).status_code
        == 200
    )

    body = _export(client, scene.task_id).text

    assert "| 人工复核 | 已确认 |" in body
    assert "- **复核意见**：已与业务确认，接受该条款" in body
    assert "- **复核时间**" in body
    # 人工等级只在 MODIFIED 出现
    assert "- **人工风险等级**" not in body


def test_the_report_shows_a_rejected_review(client: TestClient, scene: Scenario) -> None:
    risk_id = _first_risk_id(client, scene)
    _review(client, scene.task_id, risk_id, review_status="REJECTED", review_comment="属于正常业务条款")

    assert "| 人工复核 | 已驳回 |" in _export(client, scene.task_id).text


def test_the_report_shows_a_modified_review_with_the_human_level(
    client: TestClient, scene: Scenario
) -> None:
    risk_id = _first_risk_id(client, scene)
    _review(
        client,
        scene.task_id,
        risk_id,
        review_status="MODIFIED",
        risk_level="LOW",
        review_comment="等级下调",
    )

    body = _export(client, scene.task_id).text

    assert "| 人工复核 | 已修改 |" in body
    assert "- **人工风险等级**：LOW" in body
    assert "- **复核意见**：等级下调" in body


def test_a_reviewed_risk_is_still_counted_in_the_overview(client: TestClient, scene: Scenario) -> None:
    """**概览统计的是 AI 发现了什么。**

    把一条风险驳回，概览的条数与等级分布**一个都不许变** ——
    否则报告开头那句话就从"发现了什么"变成了"裁决后剩下什么"。
    """
    before = _export(client, scene.task_id).text
    risk_id = _first_risk_id(client, scene)
    _review(client, scene.task_id, risk_id, review_status="REJECTED")
    after = _export(client, scene.task_id).text

    overview_before = next(line for line in before.splitlines() if line.startswith("本次审查共发现"))
    overview_after = next(line for line in after.splitlines() if line.startswith("本次审查共发现"))

    assert overview_before == overview_after
    assert "| 合计 | 4 |" in after


def test_the_report_never_names_a_reviewer(client: TestClient, scene: Scenario) -> None:
    """``reviewer_id`` 恒为 NULL —— 报告里不许出现任何编造的用户名。"""
    risk_id = _first_risk_id(client, scene)
    _review(client, scene.task_id, risk_id, review_status="CONFIRMED", review_comment="同意")

    body = _export(client, scene.task_id).text

    for made_up in ("系统用户", "未知用户", "默认复核人", "当前用户", "复核人："):
        assert made_up not in body


def test_one_tasks_review_does_not_leak_into_another_tasks_report(
    client: TestClient, scene: Scenario
) -> None:
    """同合同两次审查：复核了 A 的风险，B 的报告里那条**仍然是待复核**。"""
    _review(client, scene.task_id, _first_risk_id(client, scene), review_status="REJECTED")
    other_risk_id = _risk_ids(scene.other_task_id)[0]

    mine = _export(client, scene.task_id).text
    theirs = _export(client, scene.other_task_id).text

    assert "| 人工复核 | 已驳回 |" in mine
    assert "| 人工复核 | 待复核 |" in theirs
    assert "已驳回" not in theirs

    # 另一条路：复核 B 也不影响 A
    _review(client, scene.other_task_id, other_risk_id, review_status="CONFIRMED")
    assert "| 人工复核 | 已驳回 |" in _export(client, scene.task_id).text


def test_reviewing_does_not_change_the_task_or_the_error_semantics(
    client: TestClient, scene: Scenario
) -> None:
    """复核不碰 ``review_task`` 的任何字段，也不改变导出接口的门禁语义。"""
    before = _query(
        "SELECT current_stage, status, finished_at, risk_level_final, conclusion, summary "
        "FROM review_task WHERE id = %s",
        (scene.task_id,),
    )[0]

    _review(client, scene.task_id, _first_risk_id(client, scene), review_status="CONFIRMED")

    after = _query(
        "SELECT current_stage, status, finished_at, risk_level_final, conclusion, summary "
        "FROM review_task WHERE id = %s",
        (scene.task_id,),
    )[0]
    assert after == before == ("REVIEWED", "pending", None, None, None, None)

    # 门禁与 404 语义不变
    assert _export(client, 99999999).status_code == 404
    unfinished = _build_scenario(stage="CLAUSED", contract_no="RPT-P134")
    assert _export(client, unfinished.task_id).status_code == 409
