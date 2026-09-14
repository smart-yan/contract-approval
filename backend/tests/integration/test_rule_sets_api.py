"""``GET /api/v1/rule-sets`` 的集成测试（**需要真实 MySQL**）。

为什么放在 integration
---------------------
这里断言的是**数据库层面的选取规则**：哪一套规则集算"当前启用"、
哪些规则会被过滤掉、顺序由谁决定。SQLite 上跑不出同样的语义。

隔离方式
--------
* 用例自己插入的规则集一律用 ``SALES`` 合同类型 —— seed 只建了 ``PURCHASE``，
  因此**不会干扰任何依赖 PURCHASE 规则集的既有测试**（上传、并发、幂等）。
* 收尾按外键依赖的反序删除（``review_rule`` → ``review_rule_set``，FK 是 RESTRICT）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator

import pymysql
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

PREFIX = "IT-RULESET-"


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
    """删掉本模块插入的规则集（先删规则 —— FK 是 RESTRICT）。"""
    ids = [row[0] for row in _query("SELECT id FROM review_rule_set WHERE name LIKE %s", (f"{PREFIX}%",))]
    if not ids:
        return
    placeholders = ",".join(["%s"] * len(ids))
    _query(f"DELETE FROM review_rule WHERE rule_set_id IN ({placeholders})", tuple(ids))
    _query(f"DELETE FROM review_rule_set WHERE id IN ({placeholders})", tuple(ids))


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
def _insert_rule_set(*, is_active: bool = True, contract_type: str = "SALES", version: str = "v1") -> int:
    _query(
        "INSERT INTO review_rule_set "
        "(name, contract_type, version, is_active, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, NOW(3), NOW(3))",
        (f"{PREFIX}{uuid.uuid4().hex[:8]}", contract_type, version, 1 if is_active else 0),
    )
    return _scalar("SELECT MAX(id) FROM review_rule_set")


def _insert_rule(
    rule_set_id: int,
    *,
    rule_code: str,
    sort_order: int,
    is_active: bool = True,
    rule_type: str = "KEYWORD",
    expression: dict | None = None,
    target_clause_types: list[str] | None = None,
) -> int:
    _query(
        "INSERT INTO review_rule "
        "(rule_set_id, rule_code, rule_name, dimension, rule_type, expression, "
        " target_clause_types, severity, is_active, sort_order, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(3), NOW(3))",
        (
            rule_set_id,
            rule_code,
            f"规则 {rule_code}",
            "测试维度",
            rule_type,
            json.dumps(expression if expression is not None else {"keywords": ["测试"], "logic": "ANY"}),
            json.dumps(target_clause_types) if target_clause_types is not None else None,
            "HIGH",
            1 if is_active else 0,
            sort_order,
        ),
    )
    return _scalar("SELECT MAX(id) FROM review_rule")


# --------------------------------------------------------------------------- #
# 1. seed 数据：指定 contract_type 返回正确的启用规则集
# --------------------------------------------------------------------------- #
def test_returns_the_seeded_purchase_rule_set(client: TestClient) -> None:
    response = client.get("/api/v1/rule-sets", params={"contract_type": "PURCHASE"})

    assert response.status_code == 200
    body = response.json()
    assert body["contract_type"] == "PURCHASE"
    assert body["rule_set"] is not None
    assert body["rule_set"]["name"] == "采购合同审查清单 v1"
    assert body["rule_set"]["version"] == "v1"


def test_seeded_purchase_rules_are_complete_and_ordered(client: TestClient) -> None:
    rules = client.get("/api/v1/rule-sets", params={"contract_type": "PURCHASE"}).json()["rules"]

    assert [r["rule_code"] for r in rules] == [
        "IP_OWNER_SUPPLIER_001",
        "LIAB_UNLIMITED_001",
        "PAY_PREPAY_RATIO_001",
    ], "按 sort_order 升序（seed 里是 10 / 30 / 50）"


# --------------------------------------------------------------------------- #
# 2 / 3. 不存在适用规则集
# --------------------------------------------------------------------------- #
def test_missing_rule_set_returns_null_not_an_error(client: TestClient) -> None:
    """没有启用的规则集**不是错误**（架构裁决：没有规则集不能阻止上传）。

    因此是 200 + ``rule_set=null`` + 空数组，而不是 404。
    """
    response = client.get("/api/v1/rule-sets", params={"contract_type": "SERVICE"})

    assert response.status_code == 200
    body = response.json()
    assert body["contract_type"] == "SERVICE"
    assert body["rule_set"] is None
    assert body["rules"] == []


# --------------------------------------------------------------------------- #
# 4. inactive rule set 不返回
# --------------------------------------------------------------------------- #
def test_inactive_rule_set_is_not_returned(client: TestClient) -> None:
    """只插入一套**停用**的规则集 ⇒ 等同于"没有规则集"。"""
    _insert_rule_set(is_active=False)

    body = client.get("/api/v1/rule-sets", params={"contract_type": "SALES"}).json()

    assert body["rule_set"] is None
    assert body["rules"] == []


def test_active_rule_set_wins_over_a_newer_inactive_one(client: TestClient) -> None:
    """启用与否优先于"谁更新" —— 新插入一条停用的不能顶掉已启用的。"""
    active_id = _insert_rule_set(is_active=True, version="v-active")
    _insert_rule_set(is_active=False, version="v-inactive-but-newer")

    body = client.get("/api/v1/rule-sets", params={"contract_type": "SALES"}).json()

    assert body["rule_set"]["id"] == active_id
    assert body["rule_set"]["version"] == "v-active"


# --------------------------------------------------------------------------- #
# 5. inactive rule 不返回
# --------------------------------------------------------------------------- #
def test_inactive_rule_is_not_returned(client: TestClient) -> None:
    rule_set_id = _insert_rule_set()
    _insert_rule(rule_set_id, rule_code="ON_001", sort_order=10)
    _insert_rule(rule_set_id, rule_code="OFF_001", sort_order=20, is_active=False)

    rules = client.get("/api/v1/rule-sets", params={"contract_type": "SALES"}).json()["rules"]

    assert [r["rule_code"] for r in rules] == ["ON_001"], "停用的规则不能出现在结果里"


# --------------------------------------------------------------------------- #
# 6. rules 按 sort_order 返回
# --------------------------------------------------------------------------- #
def test_rules_are_ordered_by_sort_order_not_insertion_order(client: TestClient) -> None:
    """**故意乱序插入**，断言出来是按 sort_order 排的。"""
    rule_set_id = _insert_rule_set()
    _insert_rule(rule_set_id, rule_code="C_30", sort_order=30)
    _insert_rule(rule_set_id, rule_code="A_10", sort_order=10)
    _insert_rule(rule_set_id, rule_code="B_20", sort_order=20)

    rules = client.get("/api/v1/rule-sets", params={"contract_type": "SALES"}).json()["rules"]

    assert [r["rule_code"] for r in rules] == ["A_10", "B_20", "C_30"]
    assert [r["sort_order"] for r in rules] == [10, 20, 30]


def test_rules_with_equal_sort_order_are_deterministic(client: TestClient) -> None:
    """sort_order 相同时用 id 兜底 —— 顺序必须确定，不能依赖数据库返回次序。"""
    rule_set_id = _insert_rule_set()
    first = _insert_rule(rule_set_id, rule_code="SAME_1", sort_order=10)
    second = _insert_rule(rule_set_id, rule_code="SAME_2", sort_order=10)

    rules = client.get("/api/v1/rule-sets", params={"contract_type": "SALES"}).json()["rules"]

    assert [r["id"] for r in rules] == [first, second]


# --------------------------------------------------------------------------- #
# 7. Schema 序列化：JSON 契约原样透传
# --------------------------------------------------------------------------- #
def test_expression_and_target_clause_types_are_passed_through(client: TestClient) -> None:
    """``expression`` / ``target_clause_types`` 按数据库里的 JSON 原样返回，不被重构。"""
    rule_set_id = _insert_rule_set()
    expression = {"keywords": ["无限责任", "全部损失"], "logic": "ANY"}
    _insert_rule(
        rule_set_id,
        rule_code="PASS_001",
        sort_order=10,
        expression=expression,
        target_clause_types=["LIABILITY"],
    )

    rule = client.get("/api/v1/rule-sets", params={"contract_type": "SALES"}).json()["rules"][0]

    assert rule["expression"] == expression
    assert rule["target_clause_types"] == ["LIABILITY"]


def test_threshold_expression_shape_survives(client: TestClient) -> None:
    rule_set_id = _insert_rule_set()
    _insert_rule(
        rule_set_id,
        rule_code="TH_001",
        sort_order=10,
        rule_type="THRESHOLD",
        expression={"field": "prepay_ratio", "op": "gt", "value": 0.3},
        target_clause_types=["AMOUNT_PAYMENT"],
    )

    rule = client.get("/api/v1/rule-sets", params={"contract_type": "SALES"}).json()["rules"][0]

    assert rule["expression"] == {"field": "prepay_ratio", "op": "gt", "value": 0.3}
    assert rule["rule_type"] == "THRESHOLD"


def test_rule_without_target_clause_types_returns_null(client: TestClient) -> None:
    """``target_clause_types`` 为空表示**不限**，必须原样给 null 而不是 []。"""
    rule_set_id = _insert_rule_set()
    _insert_rule(rule_set_id, rule_code="ANY_001", sort_order=10, target_clause_types=None)

    rule = client.get("/api/v1/rule-sets", params={"contract_type": "SALES"}).json()["rules"][0]

    assert rule["target_clause_types"] is None


def test_response_contains_every_contract_field(client: TestClient) -> None:
    """字段集合钉死 —— 少一个字段 Agent 侧就会缺信息。"""
    body = client.get("/api/v1/rule-sets", params={"contract_type": "PURCHASE"}).json()

    assert set(body) == {"contract_type", "rule_set", "rules"}
    assert set(body["rule_set"]) == {"id", "name", "contract_type", "version", "description"}
    assert set(body["rules"][0]) == {
        "id",
        "rule_code",
        "rule_name",
        "dimension",
        "description",
        "rule_type",
        "expression",
        "target_clause_types",
        "severity",
        "legal_basis",
        "suggestion_template",
        "sort_order",
    }


def test_dimension_is_returned_verbatim(client: TestClient) -> None:
    """``dimension`` 保持数据库现有值（当前 seed 用中文展示名），不做码值统一。"""
    rules = client.get("/api/v1/rule-sets", params={"contract_type": "PURCHASE"}).json()["rules"]

    assert {r["dimension"] for r in rules} == {"知识产权", "违约责任", "金额支付"}


# --------------------------------------------------------------------------- #
# 8. 参数校验与只读性
# --------------------------------------------------------------------------- #
def test_invalid_contract_type_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/rule-sets", params={"contract_type": "NOT_A_TYPE"})

    assert response.status_code == 422


def test_contract_type_is_required(client: TestClient) -> None:
    assert client.get("/api/v1/rule-sets").status_code == 422


def test_endpoint_is_read_only(client: TestClient) -> None:
    """只读接口不得改动任何规则数据。"""
    before = (_scalar("SELECT COUNT(*) FROM review_rule_set"), _scalar("SELECT COUNT(*) FROM review_rule"))

    client.get("/api/v1/rule-sets", params={"contract_type": "PURCHASE"})

    after = (_scalar("SELECT COUNT(*) FROM review_rule_set"), _scalar("SELECT COUNT(*) FROM review_rule"))
    assert before == after


def test_repeated_calls_are_stable(client: TestClient) -> None:
    first = client.get("/api/v1/rule-sets", params={"contract_type": "PURCHASE"}).json()
    second = client.get("/api/v1/rule-sets", params={"contract_type": "PURCHASE"}).json()

    assert first == second
