"""``PATCH /api/v1/review-tasks/{task_id}/risks/{risk_id}`` 的集成测试（**需要真实 MySQL**）。

这里断言的是**只有真实数据库 + 真实 ASGI 才能验**的四件事：

1. **双 id 隔离** —— ``risk_item.id`` 是全局主键。拿 A 任务的路径去改 B 任务的风险
   必须失败，**且 B 那行一个字都不能变**
2. **状态矩阵** —— PENDING 只能出不能进；已复核的三种状态可互相修改
3. **AI 事实字段不可改** —— 复核之后 ``risk_title`` / ``reason`` / ``original_text`` /
   ``paragraph_index`` / 外键全部原样
4. **事务边界** —— 成功则提交、异常则回滚；复核**不触碰** ``review_task`` 的任何字段

隔离方式：自造 ``RRV-`` 前缀的合同 + ``RRV-TASK-`` 前缀的幂等键，收尾按外键反序删除。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pymysql
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.base import UTC_SESSION_INIT_COMMAND
from app.main import app
from app.utils.datetime_utils import utcnow

PREFIX = "RRV-"
TASK_KEY_PREFIX = "RRV-TASK-"

#: 复核接口**绝不允许**改动的列 —— 复核前后逐列比对
AI_FACT_COLUMNS = (
    "task_id",
    "contract_id",
    "clause_id",
    "rule_id",
    "risk_code",
    "risk_title",
    "dimension",
    "source",
    "reason",
    "legal_basis",
    "original_text",
    "paragraph_index",
    "locator_type",
    "anchor_method",
)


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
        # 与应用的引擎同一条会话时区约定（见 app/db/base.build_connect_args）：
        # 裸连接不设它，``NOW(3)`` 写进去的就是本地时间，而应用按 UTC 解释
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
    """同一个合同、同一个附件下**两次审查**，各自带一条风险。"""

    contract_id: int
    file_id: int
    task_id: int
    other_task_id: int
    risk_id: int
    other_risk_id: int


def _build_scenario(*, stage: str = "REVIEWED") -> Scenario:
    contract_id = _execute(
        "INSERT INTO contract "
        "(contract_no, title, contract_type, source, status, created_at, updated_at) "
        "VALUES (%s, '复核测试合同', 'PURCHASE', 'UPLOAD', 'PENDING', NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:10]}",),
    )
    file_id = _execute(
        "INSERT INTO contract_file "
        "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
        " created_at, updated_at) "
        "VALUES (%s, 'contract.docx', '.docx', 1024, %s, 'x', 0, 'PARSED', NOW(3), NOW(3))",
        (contract_id, uuid.uuid4().hex + uuid.uuid4().hex),
    )
    task_id = _insert_task(contract_id, file_id, stage=stage)
    other_task_id = _insert_task(contract_id, file_id, stage="REVIEWED")

    risk_id = _insert_risk(task_id, contract_id, title="知识产权归属相对方")
    other_risk_id = _insert_risk(other_task_id, contract_id, title="别的任务的风险")

    return Scenario(
        contract_id=contract_id,
        file_id=file_id,
        task_id=task_id,
        other_task_id=other_task_id,
        risk_id=risk_id,
        other_risk_id=other_risk_id,
    )


def _insert_task(contract_id: int, file_id: int, *, stage: str) -> int:
    return _execute(
        "INSERT INTO review_task "
        "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
        " max_retry, idempotency_key, created_at, updated_at) "
        "VALUES (%s, %s, 'pending', %s, 0, 0, 0, 0, 3, %s, NOW(3), NOW(3))",
        (contract_id, file_id, stage, f"{TASK_KEY_PREFIX}{uuid.uuid4().hex}"),
    )


def _insert_risk(task_id: int, contract_id: int, *, title: str) -> int:
    return _execute(
        "INSERT INTO risk_item "
        "(task_id, contract_id, clause_id, rule_id, risk_code, risk_title, dimension, risk_level, source, "
        " reason, legal_basis, original_text, paragraph_index, locator_type, anchor_method, "
        " review_status, created_at, updated_at) "
        "VALUES (%s, %s, NULL, NULL, 'IP_OWNER_SUPPLIER_001', %s, '知识产权', 'HIGH', 'RULE', "
        "        '成因', '依据', '命中片段', 23, 'PARAGRAPH', 'CLAUSE_SCOPED', "
        "        'PENDING', NOW(3), NOW(3))",
        (task_id, contract_id, title),
    )


@pytest.fixture
def scene() -> Scenario:
    return _build_scenario()


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _review(client: TestClient, task_id: int, risk_id: int, /, **body):
    """``/`` 让前两个参数**只能按位置传** —— 否则 ``_review(..., task_id=1)`` 这类
    用例（正是要测"请求体里塞 task_id"）会撞上形参名。"""
    return client.patch(f"/api/v1/review-tasks/{task_id}/risks/{risk_id}", json=body)


def _risk_row(risk_id: int) -> dict:
    columns = (
        "review_status",
        "review_comment",
        "reviewer_id",
        "reviewed_at",
        "risk_level",
        *AI_FACT_COLUMNS,
    )
    row = _query(f"SELECT {', '.join(columns)} FROM risk_item WHERE id = %s", (risk_id,))[0]
    return dict(zip(columns, row, strict=True))


def _shape(message: str) -> str:
    """把消息里的数字抹掉，只剩"模板" —— 用来比较两条消息是不是同一个形状。"""
    return re.sub(r"\d+", "N", message)


def _task_row(task_id: int) -> tuple:
    return _query(
        "SELECT current_stage, status, finished_at, risk_level_final, conclusion, summary "
        "FROM review_task WHERE id = %s",
        (task_id,),
    )[0]


# --------------------------------------------------------------------------- #
# 1：正常复核（PENDING → 三种结论）
# --------------------------------------------------------------------------- #
def test_pending_to_confirmed(client: TestClient, scene: Scenario) -> None:
    response = _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED")

    assert response.status_code == 200, response.text
    assert response.json()["review_status"] == "CONFIRMED"
    assert _risk_row(scene.risk_id)["review_status"] == "CONFIRMED"


def test_pending_to_rejected(client: TestClient, scene: Scenario) -> None:
    response = _review(client, scene.task_id, scene.risk_id, review_status="REJECTED")

    assert response.status_code == 200
    assert _risk_row(scene.risk_id)["review_status"] == "REJECTED"


def test_pending_to_modified_changes_the_level(client: TestClient, scene: Scenario) -> None:
    """``MODIFIED`` 是唯一会改 ``risk_level`` 的结论。"""
    assert _risk_row(scene.risk_id)["risk_level"] == "HIGH"

    response = _review(
        client, scene.task_id, scene.risk_id, review_status="MODIFIED", risk_level="LOW"
    )

    assert response.status_code == 200
    row = _risk_row(scene.risk_id)
    assert row["review_status"] == "MODIFIED"
    assert row["risk_level"] == "LOW"


def test_review_comment_is_persisted(client: TestClient, scene: Scenario) -> None:
    response = _review(
        client,
        scene.task_id,
        scene.risk_id,
        review_status="CONFIRMED",
        review_comment="已与业务确认，接受该条款",
    )

    assert response.status_code == 200
    assert _risk_row(scene.risk_id)["review_comment"] == "已与业务确认，接受该条款"


def test_review_comment_is_optional(client: TestClient, scene: Scenario) -> None:
    assert _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED").status_code == 200
    assert _risk_row(scene.risk_id)["review_comment"] is None


def test_reviewed_at_is_generated_by_the_server(client: TestClient, scene: Scenario) -> None:
    """服务端取 UTC 当前时间，且是 **naive**（项目统一口径）。"""
    before = utcnow()
    _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED")

    reviewed_at = _risk_row(scene.risk_id)["reviewed_at"]
    assert reviewed_at.tzinfo is None, "库里存的是 naive UTC，不是 aware datetime"
    # 夹具连接已钉死 UTC 会话时区，因此这里可以直接与 utcnow() 比
    assert -5 <= (reviewed_at - before).total_seconds() <= 60


def test_reviewer_id_stays_null(client: TestClient, scene: Scenario) -> None:
    """没有 ``sys_user`` 表、没有登录体系 —— 服务端**不伪造**一个用户 id 来填这一列。"""
    response = _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED")

    assert response.json()["reviewer_id"] is None
    assert _risk_row(scene.risk_id)["reviewer_id"] is None


# --------------------------------------------------------------------------- #
# 2：已复核状态之间可互相修改
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("first", "second", "level"),
    [
        ("CONFIRMED", "MODIFIED", "LOW"),
        ("MODIFIED", "REJECTED", None),
        ("REJECTED", "CONFIRMED", None),
        ("CONFIRMED", "REJECTED", None),
        ("REJECTED", "MODIFIED", "MEDIUM"),
        ("MODIFIED", "CONFIRMED", None),
    ],
)
def test_reviewed_states_can_change_into_each_other(
    client: TestClient, scene: Scenario, first: str, second: str, level: str | None
) -> None:
    first_body = {"review_status": first}
    if first == "MODIFIED":
        first_body["risk_level"] = "LOW"
    assert _review(client, scene.task_id, scene.risk_id, **first_body).status_code == 200

    second_body = {"review_status": second}
    if level:
        second_body["risk_level"] = level
    response = _review(client, scene.task_id, scene.risk_id, **second_body)

    assert response.status_code == 200, response.text
    assert _risk_row(scene.risk_id)["review_status"] == second


def test_leaving_modified_keeps_the_human_level(client: TestClient, scene: Scenario) -> None:
    """**方案 A 的固有结果，不是 bug。**

    ``MODIFIED`` 覆盖了 AI 原来的等级且不留痕，因此从 ``MODIFIED`` 改到别的结论时
    没有"改回去"的依据 —— 人工修订过的等级会保留下来。
    """
    _review(client, scene.task_id, scene.risk_id, review_status="MODIFIED", risk_level="LOW")
    _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED")

    row = _risk_row(scene.risk_id)
    assert row["review_status"] == "CONFIRMED"
    assert row["risk_level"] == "LOW"


# --------------------------------------------------------------------------- #
# 3：参数与状态规则
# --------------------------------------------------------------------------- #
def test_modified_without_a_level_is_rejected(client: TestClient, scene: Scenario) -> None:
    response = _review(client, scene.task_id, scene.risk_id, review_status="MODIFIED")

    assert response.status_code == 422
    assert _risk_row(scene.risk_id)["review_status"] == "PENDING", "校验失败不得留下任何改动"


@pytest.mark.parametrize("status", ["CONFIRMED", "REJECTED"])
def test_confirm_and_reject_may_not_carry_a_level(
    client: TestClient, scene: Scenario, status: str
) -> None:
    response = _review(client, scene.task_id, scene.risk_id, review_status=status, risk_level="LOW")

    assert response.status_code == 422
    assert _risk_row(scene.risk_id)["risk_level"] == "HIGH"


@pytest.mark.parametrize("prefix_status", ["PENDING", "CONFIRMED", "REJECTED", "MODIFIED"])
def test_nothing_can_go_back_to_pending(
    client: TestClient, scene: Scenario, prefix_status: str
) -> None:
    """``PENDING`` 是初始状态，**只能出不能进** —— 回到它等于抹掉复核痕迹。"""
    if prefix_status != "PENDING":
        body = {"review_status": prefix_status}
        if prefix_status == "MODIFIED":
            body["risk_level"] = "LOW"
        assert _review(client, scene.task_id, scene.risk_id, **body).status_code == 200

    response = _review(client, scene.task_id, scene.risk_id, review_status="PENDING")

    assert response.status_code == 422
    assert _risk_row(scene.risk_id)["review_status"] == prefix_status


def test_an_unknown_status_is_rejected(client: TestClient, scene: Scenario) -> None:
    response = _review(client, scene.task_id, scene.risk_id, review_status="APPROVED")

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# 4：AI 事实字段与任务状态都不可动
# --------------------------------------------------------------------------- #
def test_reviewing_does_not_touch_any_ai_fact_column(client: TestClient, scene: Scenario) -> None:
    """复核只写复核列 —— AI 产出的事实一个都不许变。"""
    before = _risk_row(scene.risk_id)

    _review(
        client,
        scene.task_id,
        scene.risk_id,
        review_status="MODIFIED",
        risk_level="LOW",
        review_comment="等级下调",
    )

    after = _risk_row(scene.risk_id)
    for column in AI_FACT_COLUMNS:
        assert after[column] == before[column], f"{column} 被复核改动了"


@pytest.mark.parametrize(
    "field",
    ["risk_title", "reason", "legal_basis", "original_text", "paragraph_index", "task_id", "contract_id"],
)
def test_sending_an_ai_fact_field_is_rejected(client: TestClient, scene: Scenario, field: str) -> None:
    """这些字段连传都传不进来（``extra="forbid"``）。

    静默忽略比报错更糟：调用方会拿到 200 并以为改成功了。
    """
    response = _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED", **{field: 1})

    assert response.status_code == 422
    assert _risk_row(scene.risk_id)["review_status"] == "PENDING"


def test_reviewing_does_not_touch_the_task(client: TestClient, scene: Scenario) -> None:
    """**人工复核不是流程的下一阶段。**

    不推 ``current_stage``、不动状态机、不设 ``finished_at``、
    更不写 ``risk_level_final`` / ``conclusion``（那是 §11.2 的评分，P12 已裁决
    Backend 不生成审查结论）。
    """
    before = _task_row(scene.task_id)

    _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED")

    assert _task_row(scene.task_id) == before == ("REVIEWED", "pending", None, None, None, None)


# --------------------------------------------------------------------------- #
# 5：脏 id 与越权跨任务
# --------------------------------------------------------------------------- #
def test_an_unknown_risk_id_is_a_404(client: TestClient, scene: Scenario) -> None:
    response = _review(client, scene.task_id, 99999999, review_status="CONFIRMED")

    assert response.status_code == 404
    assert response.json()["code"] == "RISK_NOT_FOUND"


def test_another_tasks_risk_looks_exactly_like_a_missing_one(
    client: TestClient, scene: Scenario
) -> None:
    """**本文件最重要的一条。**

    拿 A 任务的路径去改 B 任务的风险：必须失败，**而且和"这个 id 根本不存在"
    报得一模一样** —— 分开表达等于告诉调用方"这个 id 在别处是存在的"。
    """
    foreign = _review(client, scene.other_task_id, scene.risk_id, review_status="REJECTED")
    missing = _review(client, scene.task_id, 99999999, review_status="REJECTED")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"] == "RISK_NOT_FOUND"

    # 两条消息是**同一个模板**（把数字换成占位符后逐字相同）——
    # 措辞里没有任何"它其实属于别的任务"的痕迹
    assert _shape(foreign.json()["message"]) == _shape(missing.json()["message"])

    # 更直接的泄露检查：foreign 这次请求的真实归属是 scene.task_id，
    # 响应里不该出现它
    assert str(scene.task_id) not in foreign.json()["message"]

    # 而且**两边都没有被改动**
    assert _risk_row(scene.risk_id)["review_status"] == "PENDING"
    assert _risk_row(scene.other_risk_id)["review_status"] == "PENDING"


def test_a_missing_task_is_a_404(client: TestClient, scene: Scenario) -> None:
    response = _review(client, 99999999, scene.risk_id, review_status="CONFIRMED")

    assert response.status_code == 404
    assert response.json()["code"] == "TASK_NOT_FOUND"


def test_the_other_task_can_review_its_own_risk(client: TestClient, scene: Scenario) -> None:
    """反向确认：隔离是"按任务"，不是"只有第一个任务能用"。"""
    response = _review(client, scene.other_task_id, scene.other_risk_id, review_status="CONFIRMED")

    assert response.status_code == 200
    assert _risk_row(scene.other_risk_id)["review_status"] == "CONFIRMED"
    assert _risk_row(scene.risk_id)["review_status"] == "PENDING"


# --------------------------------------------------------------------------- #
# 6：阶段门禁
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stage", ["UPLOADED", "PARSED", "CLAUSED", "SOMETHING_NEW"])
def test_an_unfinished_task_is_a_409(client: TestClient, stage: str) -> None:
    """``CLAUSED`` 尤其关键：那时文档层已落库、风险**还没写** —— 没有可复核的对象。"""
    scenario = _build_scenario(stage=stage)

    response = _review(client, scenario.task_id, scenario.risk_id, review_status="CONFIRMED")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "RISK_REVIEW_NOT_READY"
    assert _risk_row(scenario.risk_id)["review_status"] == "PENDING"


def test_the_gate_does_not_leak_whether_the_risk_exists(client: TestClient) -> None:
    """门禁先于风险查找：未审完的任务里，真 id 与假 id 都得到同一个 409。"""
    scenario = _build_scenario(stage="CLAUSED")

    real = _review(client, scenario.task_id, scenario.risk_id, review_status="CONFIRMED")
    fake = _review(client, scenario.task_id, 99999999, review_status="CONFIRMED")

    assert real.status_code == fake.status_code == 409
    assert real.json()["message"] == fake.json()["message"]


# --------------------------------------------------------------------------- #
# 7：事务与幂等
# --------------------------------------------------------------------------- #
def test_the_review_is_committed(client: TestClient, scene: Scenario) -> None:
    """走 ``session_scope`` 的正常路径必须真的提交（另开连接查得到）。"""
    _review(client, scene.task_id, scene.risk_id, review_status="REJECTED")

    assert _risk_row(scene.risk_id)["review_status"] == "REJECTED"


def test_repeating_the_same_review_is_idempotent(client: TestClient, scene: Scenario) -> None:
    """同值重复提交结果相同，**不产生新行**，不需要 Idempotency-Key。"""
    before = _query("SELECT COUNT(*) FROM risk_item WHERE task_id = %s", (scene.task_id,))[0][0]

    first = _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED")
    second = _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED")

    after = _query("SELECT COUNT(*) FROM risk_item WHERE task_id = %s", (scene.task_id,))[0][0]
    assert first.status_code == second.status_code == 200
    assert first.json()["review_status"] == second.json()["review_status"] == "CONFIRMED"
    assert before == after == 1


def test_a_failure_after_the_update_rolls_everything_back(
    client: TestClient, scene: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**回滚验证。**

    在 ``flush()`` **之后**（UPDATE 已经发给 MySQL）、``commit`` 之前注入一个异常，
    然后确认那一行**一个字节都没变**。

    注入点选 ``logger.info``：它是 flush 之后唯一还会执行的东西。换成"在改动前失败"
    就证明不了任何事 —— 那种情况下本来就没有东西需要回滚。
    """
    import app.services.risk_review as module

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("注入的故障：flush 之后、commit 之前")

    before = _risk_row(scene.risk_id)
    monkeypatch.setattr(module.logger, "info", _boom)

    with pytest.raises(RuntimeError, match="注入的故障"):
        _review(client, scene.task_id, scene.risk_id, review_status="REJECTED")

    assert _risk_row(scene.risk_id) == before


# --------------------------------------------------------------------------- #
# 8：复核结果能被工作台读到（P13-2 的读路径）
# --------------------------------------------------------------------------- #
def _workbench(client: TestClient, task_id: int) -> dict:
    response = client.get(f"/api/v1/review-tasks/{task_id}/workbench")
    assert response.status_code == 200, response.text
    return response.json()


def _workbench_risk(client: TestClient, task_id: int, risk_id: int) -> dict:
    risks = _workbench(client, task_id)["risks"]
    return next(risk for risk in risks if risk["risk_id"] == risk_id)


def test_the_workbench_reflects_the_review(client: TestClient, scene: Scenario) -> None:
    """写完能读到 —— 否则 P13-3 的复核 UI 刷新后看不到自己刚做的结论。"""
    _review(
        client,
        scene.task_id,
        scene.risk_id,
        review_status="MODIFIED",
        risk_level="LOW",
        review_comment="等级下调",
    )

    risk = _workbench_risk(client, scene.task_id, scene.risk_id)

    assert risk["review_status"] == "MODIFIED"
    assert risk["review_comment"] == "等级下调"
    assert risk["reviewed_at"] is not None
    assert risk["reviewer_id"] is None
    assert risk["risk_level"] == "LOW"


def test_the_workbench_serialises_reviewed_at_like_every_other_timestamp(
    client: TestClient, scene: Scenario
) -> None:
    """``reviewed_at`` 沿用项目既有的 datetime 序列化 —— **不发明新格式**。

    项目统一存 naive UTC，因此 JSON 里是 ``2026-09-16T08:12:34.567``：
    没有 ``Z``、也没有 ``+08:00``。前端按既有方式补 ``Z`` 再解析。
    """
    _review(client, scene.task_id, scene.risk_id, review_status="CONFIRMED")

    reviewed_at = _workbench_risk(client, scene.task_id, scene.risk_id)["reviewed_at"]

    assert isinstance(reviewed_at, str)
    assert "T" in reviewed_at
    assert not reviewed_at.endswith("Z"), "naive UTC 不带 Z"
    assert "+" not in reviewed_at and "-" not in reviewed_at[10:], "不带时区偏移"
    # 与同一响应里既有的时间字段同一种形状
    created_at = _workbench(client, scene.task_id)["task"]["created_at"]
    assert len(reviewed_at) >= len(created_at.split("T")[0]) + 9


def test_the_workbench_keeps_ai_facts_intact_after_the_review(
    client: TestClient, scene: Scenario
) -> None:
    """复核只改复核列 —— 工作台读到的 AI 事实必须与库里**逐列相同**。"""
    _review(
        client,
        scene.task_id,
        scene.risk_id,
        review_status="MODIFIED",
        risk_level="LOW",
        review_comment="改一下",
    )

    risk = _workbench_risk(client, scene.task_id, scene.risk_id)
    row = _risk_row(scene.risk_id)

    # ``task_id`` / ``contract_id`` / ``rule_id`` 是**内部外键**、
    # ``anchor_method`` 是定位中间产物 —— 工作台 DTO 刻意都不暴露（P11 冻结的契约）。
    # 因此比较两者的**交集**，而不是假设每个 AI 列都在 DTO 里。
    comparable = [column for column in AI_FACT_COLUMNS if column in risk]
    assert len(comparable) >= 8, f"可比字段太少，这条断言会失去意义：{comparable}"

    for column in comparable:
        assert risk[column] == row[column], f"{column} 与库里不一致"


def test_another_tasks_review_does_not_leak_into_the_workbench(
    client: TestClient, scene: Scenario
) -> None:
    """读了半天也要保持隔离：复核 B 的风险，A 的工作台里那条纹丝不动。"""
    _review(client, scene.other_task_id, scene.other_risk_id, review_status="REJECTED")

    mine = _workbench_risk(client, scene.task_id, scene.risk_id)
    theirs = _workbench_risk(client, scene.other_task_id, scene.other_risk_id)

    assert mine["review_status"] == "PENDING"
    assert mine["review_comment"] is None
    assert mine["reviewed_at"] is None
    assert theirs["review_status"] == "REJECTED"

    # 而且两个工作台的风险集合本就互不相交
    assert scene.other_risk_id not in {
        risk["risk_id"] for risk in _workbench(client, scene.task_id)["risks"]
    }
