"""Golden Sample 端到端（P10-5）：**真实 Agent + 真实 Backend + 真实 MySQL**。

它与本仓其它集成测试的区别
------------------------
其它集成测试只 mock **网络层**（``httpx.MockTransport``）—— 它们验证"图接线对不对"。
这一条**不 mock 任何东西**：Backend 是一个真的 uvicorn 子进程，Agent 通过真 HTTP 调它，
数据真的落进 MySQL。因此它验证的是**跨服务契约真的对得上**：字段名、坐标、
外键解析、阶段门禁 —— 那些只有两边都真实运行时才会暴露的东西。

::

    samples/采购合同-风险版.docx
      → TestClient(Agent app)  POST /api/agent/review
        → LangGraph（真实 11 个节点）
          → 真 HTTP → uvicorn(Backend)
            → MySQL
      → 直接查库核对每一层

LLM 不参与：环境没有 API Key，``llm_review`` 按既有降级机制走 rule-only
（§9.1 第 4 道防线）。**不为了 E2E 强求真实密钥** —— 两条 HIGH 风险本来就来自规则。

隔离方式
--------
自造 ``E2E-GOLDEN-`` 前缀的合同，收尾按外键反序把 Contract/ContractFile/ReviewTask/
DocumentBlock/Clause/ContractMetadata/RiskItem 全部删掉。
（``contract_file.sha256`` 是全局 UNIQUE，不清掉会污染后续运行 —— Golden Sample
的摘要每次都是同一个。）
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pymysql
import pytest
from fastapi.testclient import TestClient

from app.core.errors import AgentErrorCode

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE = REPO_ROOT / "samples" / "采购合同-风险版.docx"
BACKEND_DIR = REPO_ROOT / "backend"

CONTRACT_NO = "E2E-GOLDEN-001"
PREFIX = "E2E-GOLDEN-"

#: 黄金样例在 seed 规则下应当命中的位置（与 ``test_golden_sample_rule_evaluation`` 同源）
EXPECTED_RULE_HITS = {
    "IP_OWNER_SUPPLIER_001": (23, "IP"),
    "LIAB_UNLIMITED_001": (30, "LIABILITY"),
}


# --------------------------------------------------------------------------- #
# 真实 Backend 子进程
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _mysql_config() -> dict[str, Any]:
    """MySQL 连接参数。

    ⚠️ 本模块跑在 **Agent** 进程里，而 ``AgentSettings`` 根本不认识 MySQL
    （Agent 不碰数据库 —— 架构红线）。因此这里直接读仓库根的 ``.env``：
    测试需要直连数据库**只为核对结果**，不构成 Agent 运行时的依赖。
    环境变量优先（与 pydantic-settings 的优先级一致）。
    """
    config: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "contract_approval",
    }
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key == "MYSQL_HOST":
                config["host"] = value
            elif key == "MYSQL_PORT":
                config["port"] = int(value)
            elif key == "MYSQL_USER":
                config["user"] = value
            elif key == "MYSQL_PASSWORD":
                config["password"] = value
            elif key == "MYSQL_DB":
                config["database"] = value

    for env_key, field in (
        ("MYSQL_HOST", "host"),
        ("MYSQL_PORT", "port"),
        ("MYSQL_USER", "user"),
        ("MYSQL_PASSWORD", "password"),
        ("MYSQL_DB", "database"),
    ):
        if env_key in os.environ:
            config[field] = int(os.environ[env_key]) if field == "port" else os.environ[env_key]
    return config


def _db_available() -> tuple[bool, str]:
    if not _mysql_config()["password"]:
        return False, ".env 中未配置 MYSQL_PASSWORD"
    try:
        _connect().close()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}（MySQL 服务未启动或库不存在？）"
    return True, ""


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(charset="utf8mb4", connect_timeout=5, autocommit=True, **_mysql_config())


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


_AVAILABLE, _REASON = _db_available()
pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=f"MySQL 不可用：{_REASON}")


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


@pytest.fixture(scope="module")
def backend_url() -> Iterator[str]:
    """真的起一个 uvicorn 子进程当 Backend（不用 ASGI transport 是因为
    Agent 与 Backend 的包都叫 ``app``，同一个进程里 import 不了两家）。"""
    port = _free_port()
    env = {**os.environ, "APP_ENV": "dev"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    try:
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError("Backend 子进程 60s 内没有就绪")
            try:
                if httpx.get(f"{url}/health", timeout=2.0).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


@pytest.fixture(scope="module")
def agent_client(backend_url: str) -> Iterator[TestClient]:
    """Agent 真实应用，``BackendClient`` 指向上面那个真 Backend。

    通过环境变量改 ``backend_base_url``（pydantic-settings 里 env 的优先级高于 .env），
    再清掉 ``get_settings`` 的 lru_cache —— lifespan 在建 ``BackendClient`` 时会重新读它。
    """
    from app.core.config import get_settings

    previous = os.environ.get("BACKEND_BASE_URL")
    os.environ["BACKEND_BASE_URL"] = backend_url
    get_settings.cache_clear()

    from app.main import app

    with TestClient(app) as client:
        yield client

    if previous is None:
        os.environ.pop("BACKEND_BASE_URL", None)
    else:
        os.environ["BACKEND_BASE_URL"] = previous
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    _purge()
    yield
    _purge()


def _post_review(client: TestClient, *, contract_no: str = CONTRACT_NO):
    with SAMPLE.open("rb") as handle:
        return client.post(
            "/api/agent/review",
            files={
                "file": (
                    SAMPLE.name,
                    handle.read(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            data={
                "contract_no": contract_no,
                "title": "采购合同-风险版",
                "contract_type": "PURCHASE",
            },
        )


# --------------------------------------------------------------------------- #
# 1：Agent 真实响应
# --------------------------------------------------------------------------- #
def test_the_agent_runs_the_whole_chain_over_real_http(agent_client: TestClient) -> None:
    response = _post_review(agent_client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workflow_status"] == "completed"
    assert body["error_code"] is None
    assert body["validation_errors"] == []
    assert body["contract_id"] and body["file_id"] and body["review_task_id"]
    assert body["sha256"] == hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    assert body["reused"] is False and body["task_reused"] is False
    assert body["parse_result"]["status"] == "PARSED"
    assert body["parse_result"]["paragraphs"], "真实文档必须解析出段落"


def test_the_agent_response_carries_no_risks_by_design(agent_client: TestClient) -> None:
    """``ReviewRunResponse`` **按设计不返回 risks** —— 风险的真源在 Backend。

    这条用例把"设计如此"钉住：哪天有人为了前端方便往响应里塞 risks，
    就等于给同一个事实造出第二个真相源（P10 的裁决明确否掉了那条路）。
    """
    body = _post_review(agent_client).json()

    assert "risks" not in body
    assert "llm_findings" not in body


# --------------------------------------------------------------------------- #
# 2：Contract / ContractFile / ReviewTask
# --------------------------------------------------------------------------- #
def test_the_three_records_are_linked_correctly(agent_client: TestClient) -> None:
    body = _post_review(agent_client).json()

    contract = _query(
        "SELECT id, contract_no, contract_type, status FROM contract WHERE id = %s",
        (body["contract_id"],),
    )[0]
    assert contract[1] == CONTRACT_NO
    assert contract[2] == "PURCHASE"

    file_row = _query(
        "SELECT contract_id, sha256, file_ext, parse_status FROM contract_file WHERE id = %s",
        (body["file_id"],),
    )[0]
    assert file_row[0] == body["contract_id"], "附件挂在合同下"
    assert file_row[1] == body["sha256"]
    assert file_row[2] == ".docx", "扩展名带前导点（P4 上传的既有写法）"
    assert file_row[3] == "PARSED", "文档层把解析状态写成了终态"

    task = _query(
        "SELECT contract_id, file_id, status, current_stage, finished_at, risk_level_final, conclusion "
        "FROM review_task WHERE id = %s",
        (body["review_task_id"],),
    )[0]
    assert task[0] == body["contract_id"], "任务挂在合同下"
    assert task[1] == body["file_id"]
    assert task[3] == "REVIEWED", "文档层 → CLAUSED，风险层 → REVIEWED"
    # P9-10 的既有语义：只推进阶段，不动状态机，也不生成评分结论
    assert task[2] == "pending"
    assert task[4] is None
    assert task[5] is None
    assert task[6] is None


# --------------------------------------------------------------------------- #
# 3：DocumentBlock —— 坐标必须真的能切回原文
# --------------------------------------------------------------------------- #
def test_every_block_matches_the_agent_parse_result(agent_client: TestClient) -> None:
    body = _post_review(agent_client).json()
    paragraphs = body["parse_result"]["paragraphs"]
    full_text = body["parse_result"]["text"]

    rows = _query(
        "SELECT order_index, paragraph_index, block_type, text, "
        "       char_start_global, char_end_global, locator_type "
        "FROM document_block WHERE contract_id = %s ORDER BY order_index",
        (body["contract_id"],),
    )

    assert len(rows) == len(paragraphs) > 0
    assert [row[0] for row in rows] == list(range(len(rows))), "order_index 连续且从 0 起"
    assert [row[1] for row in rows] == [p["index"] for p in paragraphs]

    for row, paragraph in zip(rows, paragraphs, strict=True):
        assert row[2] == paragraph["block_type"]
        assert row[3] == paragraph["text"]
        assert row[6] == "PARAGRAPH", "DOCX 的定位方式是段落级"
        # **坐标的可验证不变量**：拿库里的区间去切 Agent 的全文，必须恰好是这一段
        assert full_text[row[4] : row[5]] == row[3], f"段落 {row[0]} 的全局偏移对不上"


def test_the_rule_hits_land_on_the_expected_paragraphs(agent_client: TestClient) -> None:
    """Golden Sample 的 `段 23 / 段 30` 在**库里**确实是那两句风险原文。"""
    body = _post_review(agent_client).json()

    for paragraph_index, expected_quote in ((23, "知识产权归乙方"), (30, "全部损失")):
        text = _scalar(
            "SELECT text FROM document_block WHERE contract_id = %s AND paragraph_index = %s",
            (body["contract_id"], paragraph_index),
        )
        assert text is not None, f"段落 {paragraph_index} 没有落库"
        assert expected_quote in text


# --------------------------------------------------------------------------- #
# 4：Clause
# --------------------------------------------------------------------------- #
def test_clauses_match_the_agent_result_and_point_at_real_blocks(
    agent_client: TestClient,
) -> None:
    body = _post_review(agent_client).json()
    task_id = body["review_task_id"]

    rows = _query(
        "SELECT c.clause_no, c.title, c.clause_type, c.text, s.order_index, e.order_index "
        "FROM clause c "
        "JOIN document_block s ON c.start_block_id = s.id "
        "JOIN document_block e ON c.end_block_id = e.id "
        "WHERE c.task_id = %s ORDER BY c.id",
        (task_id,),
    )

    assert rows, "Golden Sample 必须切出条款"
    assert [row[4] for row in rows] == sorted(row[4] for row in rows), "顺序与切分顺序一致"
    assert all(row[4] <= row[5] for row in rows), "区间不倒置"

    # 命中的段落必须落在**规则限定的**条款类型里 —— 与 P7 的切分结果对得上
    for paragraph_index, clause_type in EXPECTED_RULE_HITS.values():
        covering = [row for row in rows if row[4] <= paragraph_index <= row[5]]
        assert len(covering) == 1, f"段落 {paragraph_index} 应当被恰好一个条款覆盖"
        assert covering[0][2] == clause_type


def test_the_clause_count_matches_the_agent_state(agent_client: TestClient) -> None:
    """条款数量与 Agent 侧切分出来的一致（不重不漏）。"""
    body = _post_review(agent_client).json()
    task_id = body["review_task_id"]

    stored = _scalar("SELECT COUNT(*) FROM clause WHERE task_id = %s", (task_id,))
    covering_blocks = _scalar(
        "SELECT COUNT(DISTINCT paragraph_index) FROM document_block WHERE contract_id = %s",
        (body["contract_id"],),
    )

    assert stored > 0
    assert covering_blocks >= stored, "条款的下标空间不超过块的总数"


# --------------------------------------------------------------------------- #
# 5：ContractMetadata
# --------------------------------------------------------------------------- #
def test_metadata_is_persisted_with_a_real_source_block(agent_client: TestClient) -> None:
    body = _post_review(agent_client).json()

    rows = _query(
        "SELECT m.field_key, m.field_label, m.field_value, m.value_type, m.extract_method, "
        "       b.paragraph_index "
        "FROM contract_metadata m "
        "LEFT JOIN document_block b ON m.source_block_id = b.id "
        "WHERE m.task_id = %s ORDER BY m.id",
        (body["review_task_id"],),
    )

    assert rows, "Golden Sample 应当抽出元数据"
    for field_key, field_label, field_value, value_type, extract_method, paragraph_index in rows:
        assert field_key and field_label
        assert field_value is not None
        # ⚠️ **不在这里重新定义 value_type 的取值集合**：P10-1 明确它是自由文本
        # （§7.2 只给了列注释、项目里没有对应枚举），实际数据里就有 RATIO 这类值。
        # E2E 只验证"落库了、不是空的"，取值口径以 P7 抽取器为准。
        assert value_type, f"{field_key} 的 value_type 不该为空"
        assert extract_method == "REGEX", "P7 的确定性抽取"
        assert paragraph_index is not None, f"{field_key} 应当能指回某个块"


# --------------------------------------------------------------------------- #
# 6 + 四：RiskItem 与 paragraph_index → clause_id 的**语义**验证
# --------------------------------------------------------------------------- #
def test_risks_are_persisted_and_their_clause_really_covers_them(
    agent_client: TestClient,
) -> None:
    """**本步最重要的一条**。

    不只断言 ``clause_id IS NOT NULL``，而是把整条定位链重新走一遍：

    ::

        risk.paragraph_index → DocumentBlock(file_id, paragraph_index)
                             → order_index
                             → Clause(start_block.order_index <= order_index <= end_block.order_index)

    并验证 ``clause_id`` 指的就是那个条款、它的 ``start/end`` 真的覆盖这个段落。
    """
    body = _post_review(agent_client).json()
    task_id = body["review_task_id"]

    risks = _query(
        "SELECT r.risk_title, r.dimension, r.risk_level, r.source, r.risk_code, r.paragraph_index, "
        "       r.clause_id, r.locator_type, r.review_status, r.original_text, r.task_id, r.contract_id "
        "FROM risk_item r WHERE r.task_id = %s ORDER BY r.id",
        (task_id,),
    )

    assert risks, "Golden Sample 必须产出风险"
    assert len(risks) >= len(EXPECTED_RULE_HITS), "两条规则 HIGH 风险至少都在"

    for risk in risks:
        (
            title,
            dimension,
            level,
            source,
            rule_code,
            paragraph_index,
            clause_id,
            locator_type,
            review_status,
            original_text,
            row_task_id,
            row_contract_id,
        ) = risk

        # ---- P9-10 的既有契约 ----
        assert row_task_id == task_id and row_contract_id == body["contract_id"]
        assert title and dimension and level in {"HIGH", "MEDIUM", "LOW"}
        assert source in {"RULE", "LLM", "RULE+LLM"}
        assert locator_type == "PARAGRAPH"
        assert review_status == "PENDING", "AI 刚产出，尚未人工复核"
        assert paragraph_index is not None and original_text

        # ---- 定位链的语义正确性 ----
        assert clause_id is not None, f"{rule_code or title} 的 clause_id 应当解析出来"
        order_index = _scalar(
            "SELECT order_index FROM document_block WHERE file_id = %s AND paragraph_index = %s",
            (body["file_id"], paragraph_index),
        )
        assert order_index is not None, "这个段落必须真的落库了"

        span = _query(
            "SELECT c.id, s.order_index, e.order_index, c.clause_type "
            "FROM clause c "
            "JOIN document_block s ON c.start_block_id = s.id "
            "JOIN document_block e ON c.end_block_id = e.id "
            "WHERE c.id = %s",
            (clause_id,),
        )
        assert span, f"clause_id={clause_id} 必须存在"
        (clause_row_id, start_order, end_order, _) = span[0]
        assert clause_row_id == clause_id
        assert start_order <= order_index <= end_order, (
            f"风险段落 {paragraph_index}（order_index={order_index}）"
            f"落在 clause {clause_id} 的 [{start_order}, {end_order}] 之外"
        )


def test_the_two_expected_rules_are_hits_with_the_right_clause_type(
    agent_client: TestClient,
) -> None:
    """两条规则风险都必须**真实命中**，且各自挂到对应类型的条款上。"""
    body = _post_review(agent_client).json()

    rows = _query(
        "SELECT r.risk_code, r.risk_level, r.paragraph_index, c.clause_type "
        "FROM risk_item r JOIN clause c ON r.clause_id = c.id "
        "WHERE r.task_id = %s ORDER BY r.id",
        (body["review_task_id"],),
    )
    by_code = {row[0]: row for row in rows}

    for rule_code, (paragraph_index, clause_type) in EXPECTED_RULE_HITS.items():
        assert rule_code in by_code, f"{rule_code} 应当命中（seed 规则 + Golden Sample）"
        _, level, actual_paragraph, actual_clause_type = by_code[rule_code]
        assert level == "HIGH"
        assert actual_paragraph == paragraph_index
        assert actual_clause_type == clause_type


def test_the_risk_evidence_points_back_into_the_document(agent_client: TestClient) -> None:
    """``original_text`` 是命中片段、且能在同一段落里找到（人工核对的依据）。"""
    body = _post_review(agent_client).json()

    rows = _query(
        "SELECT r.original_text, r.paragraph_index FROM risk_item r WHERE r.task_id = %s",
        (body["review_task_id"],),
    )

    for original_text, paragraph_index in rows:
        source = _scalar(
            "SELECT text FROM document_block WHERE contract_id = %s AND paragraph_index = %s",
            (body["contract_id"], paragraph_index),
        )
        assert source is not None
        assert original_text in source


# --------------------------------------------------------------------------- #
# 五：落库顺序 —— 文档层在前
# --------------------------------------------------------------------------- #
def test_the_document_layer_is_written_before_the_risks(agent_client: TestClient) -> None:
    """风险能写进去，本身就证明了文档层已经在它之前落库 ——
    Backend 的风险接口硬性要求阶段已到 ``CLAUSED``（P10-2 的前置门禁）。

    这里同时确认门禁**没有**破坏正常路径：``UPLOADED → CLAUSED → REVIEWED`` 一路走通。
    """
    body = _post_review(agent_client).json()
    task_id = body["review_task_id"]

    assert _scalar("SELECT COUNT(*) FROM document_block WHERE contract_id = %s", (body["contract_id"],)) > 0
    assert _scalar("SELECT COUNT(*) FROM clause WHERE task_id = %s", (task_id,)) > 0
    assert _scalar("SELECT COUNT(*) FROM risk_item WHERE task_id = %s", (task_id,)) > 0
    assert _scalar("SELECT current_stage FROM review_task WHERE id = %s", (task_id,)) == "REVIEWED"


# --------------------------------------------------------------------------- #
# 七：第二次运行 —— 幂等
# --------------------------------------------------------------------------- #
def test_a_second_run_does_not_duplicate_anything(agent_client: TestClient) -> None:
    """同文件 + 同配置跑第二遍：**一行都不能多，也不能被覆盖**。

    第二遍会在文档层被 409 拦下（任务阶段已是 ``REVIEWED``）——
    这是**既有幂等设计的正常结果**，不是缺陷。这里断言的是"被拒绝"以及
    "第一遍的数据一个字节都没变"。
    """
    first = _post_review(agent_client).json()
    task_id = first["review_task_id"]
    snapshot = {
        "blocks": _query(
            "SELECT id, order_index, text FROM document_block WHERE contract_id = %s ORDER BY id",
            (first["contract_id"],),
        ),
        "clauses": _query(
            "SELECT id, clause_type, text FROM clause WHERE task_id = %s ORDER BY id", (task_id,)
        ),
        "metadata": _query(
            "SELECT id, field_key, field_value FROM contract_metadata WHERE task_id = %s ORDER BY id",
            (task_id,),
        ),
        "risks": _query(
            "SELECT id, risk_code, clause_id FROM risk_item WHERE task_id = %s ORDER BY id", (task_id,)
        ),
    }

    second = _post_review(agent_client)
    body = second.json()

    assert second.status_code == 422, "第二遍是被拒绝的（409 由 Backend 判定，Agent 报 rejected）"
    assert body["workflow_status"] == "rejected"
    assert body["error_code"] in {
        "DOCUMENT_ALREADY_PERSISTED",
        AgentErrorCode.BACKEND_REJECTED.value,
        "TASK_ALREADY_PERSISTED",
    }
    # 复用语义照旧：文件和任务都命中了幂等键
    assert body["contract_id"] == first["contract_id"]
    assert body["file_id"] == first["file_id"]
    assert body["review_task_id"] == task_id

    after = {
        "blocks": _query(
            "SELECT id, order_index, text FROM document_block WHERE contract_id = %s ORDER BY id",
            (first["contract_id"],),
        ),
        "clauses": _query(
            "SELECT id, clause_type, text FROM clause WHERE task_id = %s ORDER BY id", (task_id,)
        ),
        "metadata": _query(
            "SELECT id, field_key, field_value FROM contract_metadata WHERE task_id = %s ORDER BY id",
            (task_id,),
        ),
        "risks": _query(
            "SELECT id, risk_code, clause_id FROM risk_item WHERE task_id = %s ORDER BY id", (task_id,)
        ),
    }

    assert after == snapshot, "第二遍不许新增、删除或改写任何一行"


def test_a_second_run_does_not_create_a_second_contract(agent_client: TestClient) -> None:
    _post_review(agent_client)
    _post_review(agent_client)

    assert _scalar("SELECT COUNT(*) FROM contract WHERE contract_no = %s", (CONTRACT_NO,)) == 1
    assert (
        _scalar(
            "SELECT COUNT(*) FROM contract_file WHERE sha256 = %s",
            (hashlib.sha256(SAMPLE.read_bytes()).hexdigest(),),
        )
        == 1
    )


# --------------------------------------------------------------------------- #
# 八：整套断言共用一个真实运行结果，避免重复跑图
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def golden_run(agent_client: TestClient) -> dict[str, Any]:
    """跑一次 Golden Sample，把 Agent 响应与它在库里留下的痕迹一并返回。

    单独放一个 fixture 是为了让"跑一次"与"很多条断言"分开 ——
    上面每条用例各自跑一遍图（每条都独立可读，且失败时报错定位清楚），
    这个 fixture 则给需要跨表交叉验证的用例用。
    """
    _purge()
    body = _post_review(agent_client).json()
    task_id = body["review_task_id"]
    return {
        "response": body,
        "blocks": _query(
            "SELECT order_index, paragraph_index, block_type, text, char_start_global, char_end_global "
            "FROM document_block WHERE contract_id = %s ORDER BY order_index",
            (body["contract_id"],),
        ),
        "clauses": _query(
            "SELECT c.clause_no, c.title, c.clause_type, c.text, s.order_index, e.order_index "
            "FROM clause c JOIN document_block s ON c.start_block_id = s.id "
            "JOIN document_block e ON c.end_block_id = e.id WHERE c.task_id = %s ORDER BY c.id",
            (task_id,),
        ),
        "risks": _query(
            "SELECT r.risk_code, r.risk_level, r.paragraph_index, r.clause_id, r.dimension, r.source "
            "FROM risk_item r WHERE r.task_id = %s ORDER BY r.id",
            (task_id,),
        ),
    }


def test_the_golden_run_is_self_consistent(golden_run: dict[str, Any]) -> None:
    """把三层产物放在一起交叉核对：块的坐标、条款的区间、风险的位置互相对得上。"""
    response = golden_run["response"]
    assert response["error_code"] is None

    full_text = response["parse_result"]["text"]
    paragraphs = response["parse_result"]["paragraphs"]

    assert len(golden_run["blocks"]) == len(paragraphs)
    for block in golden_run["blocks"]:
        assert full_text[block[4] : block[5]] == block[3]

    assert golden_run["clauses"], "有条款"
    assert golden_run["risks"], "有风险"
    for risk in golden_run["risks"]:
        assert risk[3] is not None, f"{risk[0]} 的 clause_id 必须解析出来"
