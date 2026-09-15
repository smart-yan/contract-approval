"""``POST /api/agent/review`` 的端到端行为。

只 mock **网络层**（``httpx.MockTransport``），不 mock Graph、不 mock 节点 ——
multipart 编码、临时文件落盘、State 流转、Conditional Edge 分流、
状态码与响应投影全部真实执行。

Agent 在这一跳上只做传输：文件与元数据原样转发给 Backend，
所有业务判断（类型/大小/幂等）都由 Backend 完成。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable, Coroutine, Iterator
from contextlib import ExitStack
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app.api.review as review_module
from app.core.config import get_settings
from app.llm.schemas import LLMRequest, LLMResult
from app.main import app
from app.rules.schemas import EvaluationFailureReason, RuleEvaluationStatus
from app.tools.backend_client import BackendClient
from tests.factories import docx_bytes


class _FakeProvider:
    """离线 provider：只记录请求，返回"没发现问题"。

    ⚠️ 必须带 ``aclose()``：lifespan 关闭时会关掉 ``app.state.llm_provider``，
    替身少了这个方法就会在 **teardown** 阶段炸掉（而不是在断言里）。
    """

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        return LLMResult(raw_text='{"findings": []}', model="fake", provider="fake")

    async def aclose(self) -> None:
        """与 :class:`~app.llm.provider.DeepSeekProvider` 同一份生命周期接口。"""


#: 节点模块对象 —— 用于把 ``evaluate_rule`` 换成 spy（见相关用例）。
#: ⚠️ 不能用字符串 monkeypatch：``nodes/__init__.py`` 重导出了同名函数，
#: ``app.graph.nodes.rule_review`` 这个名字在包命名空间里指的是**函数**而不是模块。
_RULE_REVIEW_MODULE = import_module("app.graph.nodes.rule_review")

BACKEND_BASE_URL = "http://backend.test"
EXPECTED_UPLOAD_URL = f"{BACKEND_BASE_URL}/api/v1/contracts"

#: Backend ``POST /api/v1/contracts`` 成功响应的真实形状
SUCCESS_PAYLOAD: dict[str, Any] = {
    "contract_id": 11,
    "contract_no": "HT-2026-001",
    "title": "设备采购合同",
    "contract_type": "PURCHASE",
    "contract_status": "pending",
    "file_id": 22,
    "filename": "contract.docx",
    "file_size": 1234,
    "file_type": "DOCX",
    "sha256": "a" * 64,
    "parse_status": "PENDING",
    "review_task_id": 33,
    "task_status": "pending",
    "task_stage": "UPLOADED",
    "reused": False,
    "task_reused": False,
}

#: 一份**真实**的 DOCX（P6-1 起 parse_document 会真的去解析它）
DOCX_PARAGRAPHS = ("甲方：某某科技有限公司", "第一条 本合同自双方签字之日起生效。")
DOCX_BYTES = docx_bytes(*DOCX_PARAGRAPHS)

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]

#: ``GET /api/v1/rule-sets?contract_type=PURCHASE`` 的**真实响应**（长文本已截短）
RULE_SETS_PURCHASE_PAYLOAD: dict[str, Any] = {
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
            "rule_type": "KEYWORD",
            "expression": {"keywords": ["知识产权归乙方"], "logic": "ANY"},
            "target_clause_types": ["IP"],
            "severity": "HIGH",
            "sort_order": 10,
        },
        {
            "id": 3,
            "rule_code": "LIAB_UNLIMITED_001",
            "rule_name": "我方单方承担无限责任",
            "dimension": "违约责任",
            "rule_type": "KEYWORD",
            "expression": {"keywords": ["全部损失"], "logic": "ANY"},
            "target_clause_types": ["LIABILITY"],
            "severity": "HIGH",
            "sort_order": 30,
        },
        {
            "id": 5,
            "rule_code": "PAY_PREPAY_RATIO_001",
            "rule_name": "预付款比例超过 30%",
            "dimension": "金额支付",
            "rule_type": "THRESHOLD",
            "expression": {"field": "prepay_ratio", "op": "gt", "value": 0.3},
            "target_clause_types": ["AMOUNT_PAYMENT"],
            "severity": "MEDIUM",
            "sort_order": 50,
        },
    ],
}

#: ``GET /api/v1/rule-sets?contract_type=SERVICE`` 的真实响应：没有启用的规则集
RULE_SETS_SERVICE_PAYLOAD: dict[str, Any] = {
    "contract_type": "SERVICE",
    "rule_set": None,
    "rules": [],
}


def _json_response(payload: dict[str, Any], status_code: int = 200) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


def _routes(contract_handler: Handler, rule_sets: Handler | None = None) -> Handler:
    """把 Backend 的**两个**接口分开。

    真实编排会先 ``GET /rule-sets`` 取规则集，再 ``POST /contracts`` 上传 ——
    用例自己的 handler 只关心后者，规则集默认返回 PURCHASE 的真实响应，
    需要别的行为（SERVICE / 失败 / 坏数据）时用 ``agent(..., rule_sets=...)`` 覆盖。
    """
    rules_handler = rule_sets or _json_response(RULE_SETS_PURCHASE_PAYLOAD)

    async def routed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rule-sets"):
            return await rules_handler(request)
        return await contract_handler(request)

    return routed


@pytest.fixture
def agent() -> Iterator[Callable[..., TestClient]]:
    """启动 Agent 应用，并把它的 ``BackendClient`` 换成挂了 MockTransport 的实例。

    ``app.state.backend_client`` 由 lifespan 创建；这里在启动后替换它，
    因此不需要 monkeypatch 全局、也不需要起真实 Backend ——
    这正是把依赖放进 ``app.state`` 而不是模块级单例的收益。

    :param rule_sets: 覆盖 ``GET /rule-sets`` 的行为；默认返回 PURCHASE 的真实响应。
    """
    with ExitStack() as stack:
        opened: list[httpx.AsyncClient] = []

        def _make(handler: Handler, rule_sets: Handler | None = None) -> TestClient:
            test_client = stack.enter_context(TestClient(app))
            transport = httpx.MockTransport(_routes(handler, rule_sets))
            http_client = httpx.AsyncClient(transport=transport)
            opened.append(http_client)
            app.state.backend_client = BackendClient(BACKEND_BASE_URL, client=http_client)
            return test_client

        yield _make

        for http_client in opened:
            # MockTransport 不持有连接，但显式关闭更干净
            asyncio.run(http_client.aclose())


def _post(test_client: TestClient, payload: bytes = DOCX_BYTES, contract_type: str = "PURCHASE") -> Any:
    return test_client.post(
        "/api/agent/review",
        files={
            "file": (
                "contract.docx",
                payload,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={
            "contract_no": "HT-2026-001",
            "title": "设备采购合同",
            "contract_type": contract_type,
        },
    )


# --------------------------------------------------------------------------- #
# 健康检查（lifespan 改动后的最小回归）
# --------------------------------------------------------------------------- #
def test_health_still_reports_service_state(agent: Callable[[Handler], TestClient]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不会被调用
        raise AssertionError("健康检查不应触发任何出站请求")

    test_client = agent(handler)
    response = test_client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "ai-agent"
    assert body["status"] == "ok"


# --------------------------------------------------------------------------- #
# 合法文件 —— 走完 upload → validate → parse
# --------------------------------------------------------------------------- #
def test_valid_file_runs_the_whole_workflow(agent: Callable[[Handler], TestClient]) -> None:
    requests: list[httpx.Request] = []
    bodies: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        bodies.append(await request.aread())
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler))

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_status"] == "completed"

    # 1) 确实把文件转发给了 Backend 的接入接口
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == EXPECTED_UPLOAD_URL
    assert requests[0].headers["content-type"].startswith("multipart/form-data;")
    assert b'name="contract_no"' in bodies[0]
    assert DOCX_BYTES in bodies[0], "文件字节必须原样转发"

    # 2) Backend 返回的标识与幂等信号被透传
    assert body["contract_id"] == 11
    assert body["file_id"] == 22
    assert body["review_task_id"] == 33
    assert body["sha256"] == "a" * 64
    assert body["reused"] is False
    assert body["task_reused"] is False

    # 3) 解析节点真的解析了 DOCX（P6-1 起不再是桩）
    assert body["parse_result"]["status"] == "PARSED"
    assert body["parse_result"]["parser"] == "DocxParser"
    assert body["parse_result"]["source_file_type"] == "DOCX"
    assert [p["text"] for p in body["parse_result"]["paragraphs"]] == list(DOCX_PARAGRAPHS)
    assert body["parse_result"]["text"] == "\n".join(DOCX_PARAGRAPHS)
    assert body["validation_errors"] == []
    assert body["error_code"] is None


# --------------------------------------------------------------------------- #
# 解析失败 / 空文档 —— workflow_status 必须与 parse_result 一致
# --------------------------------------------------------------------------- #
def test_unparsable_document_is_never_reported_as_completed(
    agent: Callable[[Handler], TestClient],
) -> None:
    """核心不变量：``parse_result.status == "FAILED"`` ⇒ ``workflow_status != "completed"``。

    这份文件上传能过（Backend 认它是 DOCX），但内容不是合法的 ZIP ——
    解析读不出来。工作流**没有**产出任何可用内容，就不能说它完成了。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler), payload=b"this is not a zip archive at all")

    assert response.status_code == 422, "没产出可用文档就不该是 2xx"
    body = response.json()

    assert body["parse_result"]["status"] == "FAILED"
    assert body["workflow_status"] != "completed"
    assert body["workflow_status"] == "rejected"
    assert body["error_code"] == "PARSE_FAILED"
    assert body["parse_result"]["error_code"] == "PARSE_FAILED"
    assert body["parse_result"]["paragraphs"] == []
    # 上传本身是成功的 —— 失败发生在解析，不要把它误报成上传问题
    assert body["contract_id"] == 11
    assert body["validation_errors"] == []


def test_purchase_request_fetches_the_rule_set_and_runs_every_rule(
    agent: Callable[[Handler], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**真实 HTTP 闭环**：PURCHASE → 取到 v1 + 3 条规则 → rule_review 真的跑了 3 条。

    只 mock 网络层的响应内容；取规则、映射、注入 State、逐条求值全部真实执行。
    """
    evaluations: list[Any] = []
    real_evaluate = _RULE_REVIEW_MODULE.evaluate_rule

    def spy(rule: Any, clauses: list[Any], metadata: Any = ()) -> Any:
        result = real_evaluate(rule, clauses, metadata=metadata)
        evaluations.append(result)
        return result

    monkeypatch.setattr(_RULE_REVIEW_MODULE, "evaluate_rule", spy)

    rule_set_requests: list[httpx.Request] = []

    async def rules_handler(request: httpx.Request) -> httpx.Response:
        rule_set_requests.append(request)
        return httpx.Response(200, json=RULE_SETS_PURCHASE_PAYLOAD)

    async def contract_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(contract_handler, rule_sets=rules_handler))

    # 1) 真的按 contract_type 去 Backend 取了当前启用的规则集
    assert len(rule_set_requests) == 1
    assert rule_set_requests[0].method == "GET"
    assert rule_set_requests[0].url.params["contract_type"] == "PURCHASE"

    # 2) 3 条规则**每条都被求值过**（不是"图跑到了 END"）
    assert [e.rule_code for e in evaluations] == [
        "IP_OWNER_SUPPLIER_001",
        "LIAB_UNLIMITED_001",
        "PAY_PREPAY_RATIO_001",
    ]

    # 3) 规则执行失败**保持原样**（THRESHOLD 缺 prepay_ratio，GAP-C）
    failed = [e for e in evaluations if e.status is RuleEvaluationStatus.EVALUATION_FAILED]
    assert [e.rule_code for e in failed] == ["PAY_PREPAY_RATIO_001"]
    assert failed[0].failure_reason is EvaluationFailureReason.MISSING_INPUT

    # 4) 这次请求**不是因为缺 snapshot 而被拒绝**
    assert response.status_code == 200
    body = response.json()
    assert body["workflow_status"] == "completed"
    assert body["error_code"] is None


@pytest.mark.parametrize(
    ("prepay_cell", "expected_status"),
    [
        ("30%", RuleEvaluationStatus.NOT_MATCHED),  # 恰好等于阈值，gt 不成立
        ("50%", RuleEvaluationStatus.MATCHED),  # 超过阈值 → 真的命中
    ],
)
def test_purchase_request_feeds_metadata_into_the_threshold_rule(
    agent: Callable[[Handler], TestClient],
    monkeypatch: pytest.MonkeyPatch,
    prepay_cell: str,
    expected_status: RuleEvaluationStatus,
) -> None:
    """端到端（P8-3）：DOCX 里的付款表 → P7-2 抽出比例 → THRESHOLD 得到确定结论。

    改前这条规则恒为 ``EVALUATION_FAILED``；现在它真的被比过了 ——
    而且 30% 与 50% 给出**不同**的结论，说明不是"随便给个状态"。
    """
    evaluations: list[Any] = []
    real_evaluate = _RULE_REVIEW_MODULE.evaluate_rule

    def spy(rule: Any, clauses: list[Any], metadata: Any = ()) -> Any:
        result = real_evaluate(rule, clauses, metadata=metadata)
        evaluations.append(result)
        return result

    monkeypatch.setattr(_RULE_REVIEW_MODULE, "evaluate_rule", spy)

    async def contract_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    # 一份带**真实表格**的 DOCX：付款计划表里「1. 预付款 | <比例>」
    payload = docx_bytes(
        "第一条 付款方式",
        "付款计划如下：",
        [["付款阶段", "比例"], ["1. 预付款", prepay_cell], ["2. 到货款", "60%"]],
    )

    response = _post(agent(contract_handler), payload=payload)

    threshold = next(e for e in evaluations if e.rule_code == "PAY_PREPAY_RATIO_001")
    assert threshold.status is expected_status
    assert threshold.status is not RuleEvaluationStatus.EVALUATION_FAILED, "GAP-C 已闭合"

    if expected_status is RuleEvaluationStatus.MATCHED:
        (risk,) = threshold.risks
        assert risk.risk_code == "PAY_PREPAY_RATIO_001"
        assert "预付款" in risk.quote, "定位回到付款表那一行"
        assert risk.risk_level == "MEDIUM"
    else:
        assert threshold.risks == []

    # 另外两条规则不受影响（KEYWORD 仍然照常跑）
    assert [e.rule_code for e in evaluations][:2] == [
        "IP_OWNER_SUPPLIER_001",
        "LIAB_UNLIMITED_001",
    ]
    assert response.status_code == 200
    assert response.json()["workflow_status"] == "completed"


def test_endpoint_injects_the_llm_provider_from_app_state(
    agent: Callable[[Handler], TestClient],
) -> None:
    """P9-6a：编排层把 provider 从 ``app.state`` 注入 ``ReviewContext``，节点用的是它。

    这里把 provider 换成一个离线替身，**证明注入路径真的通了**
    （换了替身就真的走替身，而不是某个藏在别处的默认实现）。
    """
    provider = _FakeProvider()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    test_client = agent(handler)  # 先启动（lifespan 会创建默认 provider）
    app.state.llm_provider = provider  # 启动后再替换 —— 与 backend_client 同一手法
    response = _post(test_client)

    assert len(provider.requests) == 1, "llm_review 用的就是注入进来的这个 provider"
    assert response.status_code == 200
    assert response.json()["workflow_status"] == "completed"


def test_endpoint_degrades_to_rule_only_when_llm_is_unavailable(
    agent: Callable[[Handler], TestClient],
) -> None:
    """LLM 不可用只是**降级**：这次审查照常完成（规则结果仍可用），不是 422。

    覆盖真实默认路径：lifespan 创建的 ``DeepSeekProvider`` 在本机未配置密钥，
    ``complete()`` 会在本地短路 —— **不会发生任何真实网络调用**。
    """
    if get_settings().llm_configured:
        pytest.skip("本机配置了 DEEPSEEK_*，这条「未配置」用例不适用")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler))

    body = response.json()
    assert response.status_code == 200, "LLM 挂了不该让整次审查失败"
    assert body["workflow_status"] == "completed"
    assert body["error_code"] is None, "降级不占用整次审查的失败通道"
    assert body["parse_result"]["status"] == "PARSED"


def test_contract_type_without_rule_set_still_completes(
    agent: Callable[[Handler], TestClient],
) -> None:
    """SERVICE：Backend 明确回答"没有启用的规则集"（200 + rule_set=null）= **正常完成**。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(
        agent(handler, rule_sets=_json_response(RULE_SETS_SERVICE_PAYLOAD)),
        contract_type="SERVICE",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_status"] == "completed"
    assert body["error_code"] is None
    assert body["parse_result"]["status"] == "PARSED"


def test_rule_set_fetch_failure_is_not_disguised_as_an_empty_rule_set(
    agent: Callable[[Handler], TestClient],
) -> None:
    """取规则失败 → **不伪装成空规则集**，也不假装审查跑完了。"""

    async def contract_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(
        agent(contract_handler, rule_sets=_json_response({"code": "INTERNAL", "message": "boom"}, 500))
    )

    assert response.status_code == 422
    body = response.json()
    assert body["workflow_status"] == "rejected"
    assert body["error_code"] == "INTERNAL"
    # 空规则集的表现会是 200 + completed + 无 error —— 这里必须不是
    assert body["error_code"] is not None


def test_rule_set_transport_failure_is_not_disguised_as_an_empty_rule_set(
    agent: Callable[[Handler], TestClient],
) -> None:
    """连不上 Backend 的规则接口 → 同样不许退化成空规则集。"""

    async def contract_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    response = _post(agent(contract_handler, rule_sets=unreachable))

    assert response.status_code == 422
    body = response.json()
    assert body["workflow_status"] == "rejected"
    assert body["error_code"] == "BACKEND_UNREACHABLE"


def test_rule_set_conversion_failure_is_not_disguised_as_an_empty_rule_set(
    agent: Callable[[Handler], TestClient],
) -> None:
    """2xx 但内容映不成快照（缺 rules / 缺 version）→ 同样不许退化成空规则集。"""

    async def contract_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    for broken in (
        {"contract_type": "PURCHASE", "rule_set": {"id": 1, "version": "v1"}},  # 缺 rules
        {"contract_type": "PURCHASE", "rule_set": {"id": 1}},  # 缺 version
        {"contract_type": "PURCHASE", "rule_set": None, "rules": [{"rule_code": "X"}]},  # null 却带规则
    ):
        response = _post(agent(contract_handler, rule_sets=_json_response(broken)))

        assert response.status_code == 422, broken
        body = response.json()
        assert body["workflow_status"] == "rejected"
        assert body["error_code"] == "BACKEND_CONTRACT_INCOMPLETE", broken


def test_empty_document_is_completed_not_rejected(
    agent: Callable[[Handler], TestClient],
) -> None:
    """空文档是**数据问题**（合同本身没内容），解析是成功的，不能说工作流失败。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler), payload=docx_bytes())

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_status"] == "completed"
    assert body["parse_result"]["status"] == "EMPTY"
    assert body["parse_result"]["text"] == ""
    assert body["error_code"] is None


# --------------------------------------------------------------------------- #
# Backend 拒绝 —— Gate 拦下，不进入解析
# --------------------------------------------------------------------------- #
def test_backend_rejection_yields_422_and_no_parse(
    agent: Callable[[Handler], TestClient],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            415,
            json={"code": "UNSUPPORTED_FORMAT", "message": "不支持的文件扩展名", "details": {}},
        )

    response = _post(agent(handler))

    assert response.status_code == 422
    body = response.json()
    assert body["workflow_status"] == "rejected"
    # 保留 Backend 的稳定错误码
    assert body["error_code"] == "UNSUPPORTED_FORMAT"
    assert body["validation_errors"]
    assert body["parse_result"] is None, "被拦下时不得进入解析阶段"
    assert body["contract_id"] is None


# --------------------------------------------------------------------------- #
# Backend 不可达
# --------------------------------------------------------------------------- #
def test_backend_unreachable_yields_422(agent: Callable[[Handler], TestClient]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    response = _post(agent(handler))

    assert response.status_code == 422
    body = response.json()
    assert body["workflow_status"] == "rejected"
    assert body["error_code"] == "BACKEND_UNREACHABLE"
    assert body["parse_result"] is None


# --------------------------------------------------------------------------- #
# 临时文件生命周期
# --------------------------------------------------------------------------- #
def test_upload_temp_file_is_removed_after_the_request(
    agent: Callable[[Handler], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """无论工作流成败，请求内落盘的临时文件都必须被删除。"""
    created: list[Path] = []
    original = review_module._spool_to_temp

    async def spy(upload: Any, suffix: str) -> Path:
        path = await original(upload, suffix)
        created.append(path)
        return path

    monkeypatch.setattr(review_module, "_spool_to_temp", spy)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler))

    assert response.status_code == 200
    assert len(created) == 1, "应当恰好落盘一个临时文件"
    assert not created[0].exists(), "请求结束后临时文件必须被删除"


def test_temp_file_is_also_removed_when_backend_rejects(
    agent: Callable[[Handler], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Path] = []
    original = review_module._spool_to_temp

    async def spy(upload: Any, suffix: str) -> Path:
        path = await original(upload, suffix)
        created.append(path)
        return path

    monkeypatch.setattr(review_module, "_spool_to_temp", spy)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(415, json={"code": "UNSUPPORTED_FORMAT", "message": "x"})

    response = _post(agent(handler))

    assert response.status_code == 422
    assert created and not created[0].exists(), "失败路径同样必须清理临时文件"


def test_agent_does_not_write_into_the_repo(
    agent: Callable[[Handler], TestClient],
) -> None:
    """Agent 不是存储层：它只用系统临时目录，绝不往仓库里写文件。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    _post(agent(handler))

    repo_root = Path(__file__).resolve().parents[3]
    agent_dir = repo_root / "ai-agent"
    stray = [p.name for p in agent_dir.rglob("agent-upload-*")]
    assert stray == [], f"Agent 目录下出现临时文件残留：{stray}"

    # 确认使用的确实是系统临时目录
    assert Path(tempfile.gettempdir()).is_dir()
