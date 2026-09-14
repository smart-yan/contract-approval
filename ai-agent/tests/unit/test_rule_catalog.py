"""规则集快照（P8-2 第一小步）：``BackendClient.get_effective_rule_set`` + ``snapshot_from_backend``。

只 mock **网络层**（``httpx.MockTransport``），映射与错误处理全部真实执行 ——
与 ``test_contract_ingest_tool.py`` 同一路子。

``PURCHASE_PAYLOAD`` 是 ``GET /api/v1/rule-sets?contract_type=PURCHASE`` 的**真实响应**，
从 P8-0 的 Backend 上原样抄下来（含真实 ``id`` 1 / 3 / 5）。
唯一改动是长文本（``description`` / ``legal_basis`` / ``suggestion_template``）被截短 ——
映射不依赖它们的长度。**不在这里改任何 Backend 契约**。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import httpx
import pytest

from app.rules.catalog import RuleSnapshotError, snapshot_from_backend
from app.rules.schemas import AgentRule, RuleSetSnapshot
from app.tools.backend_client import BackendClient, BackendRequestError

BACKEND_BASE_URL = "http://backend.test"
EXPECTED_RULE_SETS_URL = f"{BACKEND_BASE_URL}/api/v1/rule-sets"

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]

#: ``GET /api/v1/rule-sets?contract_type=PURCHASE`` 的真实响应（长文本已截短）
PURCHASE_PAYLOAD: dict[str, Any] = {
    "contract_type": "PURCHASE",
    "rule_set": {
        "id": 1,
        "name": "采购合同审查清单 v1",
        "contract_type": "PURCHASE",
        "version": "v1",
        "description": "适用于我方作为采购方的软件/货物采购合同。",
    },
    "rules": [
        {
            "id": 1,
            "rule_code": "IP_OWNER_SUPPLIER_001",
            "rule_name": "知识产权归属相对方",
            "dimension": "知识产权",
            "description": "采购场景下，若约定成果或软件的知识产权归供方所有……",
            "rule_type": "KEYWORD",
            "expression": {
                "keywords": ["知识产权归供方", "知识产权归乙方", "知识产权归供应商", "所有权归乙方"],
                "logic": "ANY",
            },
            "target_clause_types": ["IP"],
            "severity": "HIGH",
            "legal_basis": "《民法典》第 843 条",
            "suggestion_template": "建议修改为：知识产权归甲方所有。",
            "sort_order": 10,
        },
        {
            "id": 3,
            "rule_code": "LIAB_UNLIMITED_001",
            "rule_name": "我方单方承担无限责任",
            "dimension": "违约责任",
            "description": "出现「全部损失」「无限责任」等表述时……",
            "rule_type": "KEYWORD",
            "expression": {
                "keywords": ["全部损失", "无限责任", "不设上限", "承担一切责任", "赔偿全部"],
                "logic": "ANY",
            },
            "target_clause_types": ["LIABILITY"],
            "severity": "HIGH",
            "legal_basis": "《民法典》第 584 条",
            "suggestion_template": "建议修改为：累计赔偿责任以合同总金额为上限。",
            "sort_order": 30,
        },
        {
            "id": 5,
            "rule_code": "PAY_PREPAY_RATIO_001",
            "rule_name": "预付款比例超过 30%",
            "dimension": "金额支付",
            "description": "预付比例过高会显著增加我方资金占用……",
            "rule_type": "THRESHOLD",
            "expression": {"field": "prepay_ratio", "op": "gt", "value": 0.3},
            "target_clause_types": ["AMOUNT_PAYMENT"],
            "severity": "MEDIUM",
            "legal_basis": "企业采购内控通常要求预付款不超过合同总额的 30%。",
            "suggestion_template": "建议调整为：预付款比例不超过合同总额的 30%。",
            "sort_order": 50,
        },
    ],
}

#: ``contract_type=SERVICE`` 的真实响应：没有启用的规则集（**不是错误**）
SERVICE_PAYLOAD: dict[str, Any] = {"contract_type": "SERVICE", "rule_set": None, "rules": []}


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
async def make_client() -> AsyncIterator[Callable[[Handler], BackendClient]]:
    """构造一个挂了 MockTransport 的 ``BackendClient``，用例结束时关闭底层 client。"""
    opened: list[httpx.AsyncClient] = []

    def _make(handler: Handler) -> BackendClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opened.append(client)
        return BackendClient(BACKEND_BASE_URL, client=client)

    yield _make

    for client in opened:
        await client.aclose()


def _json_handler(payload: dict[str, Any], status_code: int = 200) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


# --------------------------------------------------------------------------- #
# 1~3. 正常获取：整条链路
# --------------------------------------------------------------------------- #
async def test_fetches_purchase_rule_set_end_to_end(make_client) -> None:
    """Backend HTTP → BackendClient → snapshot → list[AgentRule] 全链路。"""
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=PURCHASE_PAYLOAD)

    client = make_client(handler)

    payload = await client.get_effective_rule_set("PURCHASE")
    snapshot = snapshot_from_backend(payload)

    assert str(captured[0].url.copy_with(query=None)) == EXPECTED_RULE_SETS_URL
    assert captured[0].url.params["contract_type"] == "PURCHASE"
    assert captured[0].method == "GET"
    assert isinstance(snapshot, RuleSetSnapshot)
    assert snapshot.contract_type == "PURCHASE"
    assert [rule.rule_code for rule in snapshot.rules] == [
        "IP_OWNER_SUPPLIER_001",
        "LIAB_UNLIMITED_001",
        "PAY_PREPAY_RATIO_001",
    ]
    assert all(isinstance(rule, AgentRule) for rule in snapshot.rules)


def test_keeps_the_rule_set_version() -> None:
    snapshot = snapshot_from_backend(PURCHASE_PAYLOAD)

    assert snapshot.rule_set_version == "v1"


def test_maps_rule_fields_from_the_backend_item() -> None:
    rules = {rule.rule_code: rule for rule in snapshot_from_backend(PURCHASE_PAYLOAD).rules}

    ip_rule = rules["IP_OWNER_SUPPLIER_001"]
    assert ip_rule.rule_name == "知识产权归属相对方"
    assert ip_rule.dimension == "知识产权"
    assert ip_rule.rule_type == "KEYWORD"
    assert ip_rule.expression["keywords"][0] == "知识产权归供方"
    assert ip_rule.target_clause_types == ["IP"]
    assert ip_rule.severity == "HIGH"
    assert ip_rule.sort_order == 10

    threshold_rule = rules["PAY_PREPAY_RATIO_001"]
    assert threshold_rule.expression == {"field": "prepay_ratio", "op": "gt", "value": 0.3}


# --------------------------------------------------------------------------- #
# 4~5. 数据库身份不得进入 Agent
# --------------------------------------------------------------------------- #
def test_backend_rule_ids_do_not_leak_into_agent_rules() -> None:
    for rule in snapshot_from_backend(PURCHASE_PAYLOAD).rules:
        assert not hasattr(rule, "id")
        assert not hasattr(rule, "rule_set_id")


def test_rule_set_id_does_not_enter_the_snapshot() -> None:
    snapshot = snapshot_from_backend(PURCHASE_PAYLOAD)

    assert not hasattr(snapshot, "id")
    assert not hasattr(snapshot, "rule_set")
    assert "id" not in RuleSetSnapshot.model_fields


def test_snapshot_fields_are_frozen() -> None:
    assert set(RuleSetSnapshot.model_fields) == {"contract_type", "rule_set_version", "rules"}


# --------------------------------------------------------------------------- #
# 6. 没有规则集：是一个**正常结论**，不是错误
# --------------------------------------------------------------------------- #
def test_no_rule_set_is_represented_without_error() -> None:
    snapshot = snapshot_from_backend(SERVICE_PAYLOAD)

    assert snapshot.contract_type == "SERVICE"
    assert snapshot.rule_set_version is None
    assert snapshot.rules == []


async def test_no_rule_set_is_not_an_http_error(make_client) -> None:
    """``rule_set=null`` 走的是 200 + ``rule_set=null``，不能被当成失败。"""
    client = make_client(_json_handler(SERVICE_PAYLOAD))

    payload = await client.get_effective_rule_set("SERVICE")

    assert snapshot_from_backend(payload).rule_set_version is None


# --------------------------------------------------------------------------- #
# 7. HTTP 非 2xx
# --------------------------------------------------------------------------- #
async def test_non_2xx_raises_backend_request_error(make_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"code": "VALIDATION_ERROR", "message": "contract_type 不是合法的合同类型"},
        )

    client = make_client(handler)

    with pytest.raises(BackendRequestError) as excinfo:
        await client.get_effective_rule_set("NOPE")

    assert excinfo.value.status_code == 422
    assert excinfo.value.error_code == "VALIDATION_ERROR"
    assert "422" in str(excinfo.value)


async def test_non_json_body_raises_backend_request_error(make_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    client = make_client(handler)

    with pytest.raises(BackendRequestError):
        await client.get_effective_rule_set("PURCHASE")


async def test_2xx_with_non_json_body_raises_backend_request_error(make_client) -> None:
    """**2xx 但不是 JSON** —— 属于传输层：Backend 没给出可解释的响应体。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy login page</html>")

    client = make_client(handler)

    with pytest.raises(BackendRequestError) as excinfo:
        await client.get_effective_rule_set("PURCHASE")

    assert excinfo.value.status_code == 200
    assert excinfo.value.error_code == "BACKEND_REJECTED"


async def test_2xx_with_json_array_raises_backend_request_error(make_client) -> None:
    """**2xx 但 JSON 不是对象**（数组 / 标量）同样在传输层就被拦下。"""
    client = make_client(_json_handler([{"rule_code": "X"}]))

    with pytest.raises(BackendRequestError) as excinfo:
        await client.get_effective_rule_set("PURCHASE")

    assert excinfo.value.error_code == "BACKEND_REJECTED"


async def test_transport_failure_raises_backend_request_error(make_client) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_client(handler)

    with pytest.raises(BackendRequestError) as excinfo:
        await client.get_effective_rule_set("PURCHASE")

    assert excinfo.value.status_code is None
    assert excinfo.value.error_code == "BACKEND_UNREACHABLE"


# --------------------------------------------------------------------------- #
# 8~10. 规则映射的宽容度与白名单
# --------------------------------------------------------------------------- #
def test_unknown_rule_type_does_not_break_the_whole_snapshot() -> None:
    """``rule_type`` 在 Agent 侧是 ``str`` —— Backend 加了一种求值器，
    不该让整份快照加载失败（该由求值器给出 EVALUATION_FAILED）。"""
    payload = _with_rule(PURCHASE_PAYLOAD, rule_type="SEMANTIC")

    snapshot = snapshot_from_backend(payload)

    assert snapshot.rules[0].rule_type == "SEMANTIC"


def test_unknown_backend_fields_are_ignored_by_the_snapshot() -> None:
    payload = _with_rule(PURCHASE_PAYLOAD, created_at="2026-01-01T00:00:00", rule_set_id=1)

    rule = snapshot_from_backend(payload).rules[0]

    assert not hasattr(rule, "created_at")
    assert not hasattr(rule, "rule_set_id")


def test_rule_payload_is_not_mutated() -> None:
    payload = _with_rule(PURCHASE_PAYLOAD, created_at="2026-01-01T00:00:00")
    snapshot = {key: value for key, value in payload.items()}

    snapshot_from_backend(payload)

    assert payload == snapshot


# --------------------------------------------------------------------------- #
# 11. version 不能丢 / 不能凭空出现
# --------------------------------------------------------------------------- #
def test_missing_version_is_a_snapshot_error() -> None:
    """``rule_set`` 在但 version 没了 —— 不能静默当成"没有规则集"。"""
    payload = {**PURCHASE_PAYLOAD, "rule_set": {"id": 1, "name": "采购合同审查清单 v1"}}

    with pytest.raises(RuleSnapshotError) as excinfo:
        snapshot_from_backend(payload)

    assert "version" in str(excinfo.value)


@pytest.mark.parametrize("rule_set", [{**PURCHASE_PAYLOAD["rule_set"], "version": ""}, "v1"])
def test_invalid_rule_set_shape_is_a_snapshot_error(rule_set: Any) -> None:
    with pytest.raises(RuleSnapshotError):
        snapshot_from_backend({**PURCHASE_PAYLOAD, "rule_set": rule_set})


# --------------------------------------------------------------------------- #
# 领域不变量：版本与规则必须互相说得通
# --------------------------------------------------------------------------- #
def test_null_rule_set_with_rules_is_a_snapshot_error() -> None:
    """``rule_set=null`` 却带着规则 —— 坏响应，不能被当成"没有规则集"收下。"""
    payload = {**SERVICE_PAYLOAD, "rules": PURCHASE_PAYLOAD["rules"]}

    with pytest.raises(RuleSnapshotError) as excinfo:
        snapshot_from_backend(payload)

    assert "rules 必须为空" in str(excinfo.value)


def test_rule_set_with_version_and_no_rules_is_ok() -> None:
    """有规则集但一条启用规则都没有 —— 合法（规则集存在，只是没配规则）。"""
    payload = {**PURCHASE_PAYLOAD, "rules": []}

    snapshot = snapshot_from_backend(payload)

    assert snapshot.rule_set_version == "v1"
    assert snapshot.rules == []


def test_rule_set_with_explicit_null_version_is_a_snapshot_error() -> None:
    """``rule_set`` 在、``version`` 显式为 null —— 版本无法确定，不是"没有规则集"。"""
    payload = {**PURCHASE_PAYLOAD, "rule_set": {**PURCHASE_PAYLOAD["rule_set"], "version": None}}

    with pytest.raises(RuleSnapshotError):
        snapshot_from_backend(payload)


# --------------------------------------------------------------------------- #
# 12. 顺序保持 Backend 返回的顺序
# --------------------------------------------------------------------------- #
def test_rules_keep_the_backend_order() -> None:
    """Backend 已按 ``sort_order ASC, id ASC`` 排好，Agent 不重排。"""
    reversed_payload = {**PURCHASE_PAYLOAD, "rules": list(reversed(PURCHASE_PAYLOAD["rules"]))}

    codes = [rule.rule_code for rule in snapshot_from_backend(reversed_payload).rules]

    assert codes == ["PAY_PREPAY_RATIO_001", "LIAB_UNLIMITED_001", "IP_OWNER_SUPPLIER_001"]


# --------------------------------------------------------------------------- #
# 坏规则：整份快照失败，既不静默丢弃，也不崩溃
# --------------------------------------------------------------------------- #
def test_broken_rule_fails_the_whole_snapshot() -> None:
    """**不做"跳过坏规则、保留好规则"** —— 部分可用的规则集就是静默漏报。"""
    payload = {**PURCHASE_PAYLOAD, "rules": [*PURCHASE_PAYLOAD["rules"], {"rule_code": "NO_NAME"}]}

    with pytest.raises(RuleSnapshotError) as excinfo:
        snapshot_from_backend(payload)

    assert "NO_NAME" in str(excinfo.value)


def test_broken_rule_is_not_turned_into_an_empty_or_partial_snapshot() -> None:
    """坏规则既不能变成空快照（= 当作没有规则集），也不能被悄悄丢掉。"""
    payload = {**PURCHASE_PAYLOAD, "rules": [PURCHASE_PAYLOAD["rules"][0], {"rule_code": "BROKEN"}]}

    with pytest.raises(RuleSnapshotError):
        snapshot_from_backend(payload)


@pytest.mark.parametrize(
    "rules",
    ["not-a-list", None, {"rule_code": "X"}],
)
def test_invalid_rules_shape_is_a_snapshot_error(rules: Any) -> None:
    with pytest.raises(RuleSnapshotError):
        snapshot_from_backend({**PURCHASE_PAYLOAD, "rules": rules})


@pytest.mark.parametrize("payload", [[], "PURCHASE", None, 42])
def test_non_object_response_is_a_snapshot_error(payload: Any) -> None:
    with pytest.raises(RuleSnapshotError):
        snapshot_from_backend(payload)


def test_missing_contract_type_is_a_snapshot_error() -> None:
    payload = {key: value for key, value in PURCHASE_PAYLOAD.items() if key != "contract_type"}

    with pytest.raises(RuleSnapshotError):
        snapshot_from_backend(payload)


# --------------------------------------------------------------------------- #
def _with_rule(base: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """复制一份 payload，只改动第一条规则的若干字段。"""
    first, *rest = base["rules"]
    return {**base, "rules": [{**first, **overrides}, *rest]}
