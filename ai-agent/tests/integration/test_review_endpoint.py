"""``POST /api/agent/review`` 的端到端行为（**P14-4 起是异步启动**）。

只 mock **网络层**（``httpx.MockTransport``），不 mock Graph、不 mock 节点 ——
multipart 编码、临时文件落盘、State 流转、Conditional Edge 分流、
**后台执行**与失败上报全部真实执行。

Agent 在这一跳上只做传输：文件与元数据原样转发给 Backend，
所有业务判断（类型/大小/幂等）都由 Backend 完成。

⚠️ P14-4 改变了断言的对象
----------------------
端点现在只回 ``202 + task_id``，**响应体里不再有** ``workflow_status`` /
``parse_result``。因此"图跑成什么样"改为从 Agent **对 Backend 做了什么**观察：

* 成功 → 上传 + 写文档层 + 写风险，**且没有** ``POST .../block``
* 失败 → 有一次 ``POST .../block``，请求体里带着稳定的 ``block_reason_code``

这比断言一个内部 DTO 更接近跨服务真正可见的契约。
"""

from __future__ import annotations

import asyncio
import json
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
from app.background import BackgroundReviews
from app.core.config import get_settings
from app.llm.schemas import LLMRequest, LLMResult
from app.main import app
from app.rules.schemas import EvaluationFailureReason, RuleEvaluationStatus
from app.schemas.document import ParseResult
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


class _Backend:
    """假 Backend：记录 Agent 发给它的**每一次**请求与请求体。

    P14-4 起端点只返回 202 + task_id，图在后台跑 —— **响应体里再也没有**
    ``workflow_status`` / ``parse_result`` 了。于是"图干了什么"只能从
    **它对 Backend 做了什么**观察：上传、写文档层、写风险、以及失败时写 blocked。

    这其实更接近契约本身：跨服务真正可见的就是这些请求。
    """

    def __init__(self, *, status_code: int = 201, payload: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self._status = status_code
        self._payload = SUCCESS_PAYLOAD if payload is None else payload

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(await request.aread())
        return httpx.Response(self._status, json=self._payload)

    def of(self, suffix: str) -> list[httpx.Request]:
        """按路径后缀筛出请求（``/document``、``/risks``、``/block`` …）。"""
        return [request for request in self.requests if request.url.path.endswith(suffix)]

    @property
    def block_payloads(self) -> list[dict[str, Any]]:
        """所有 ``POST .../block`` 的请求体 —— 后台失败的**唯一**外部证据。"""
        return [
            json.loads(self.bodies[index])
            for index, request in enumerate(self.requests)
            if request.url.path.endswith("/block")
        ]

    def assert_not_blocked(self) -> None:
        """成功路径的判据：**没有**任何 block 调用。

        端点不再回 ``workflow_status``，所以"这次审查成功了"表现为
        "图跑到底，没有把任务标成阻塞"。
        """
        assert self.block_payloads == [], f"不该有 block 调用，实际：{self.block_payloads}"


def _post(
    test_client: TestClient,
    payload: bytes = DOCX_BYTES,
    contract_type: str = "PURCHASE",
    *,
    settle: bool = True,
) -> Any:
    """POST 一次审查，并**等后台图跑完**才返回（返回的是 202 响应本身）。

    P14-4 起端点立刻返回 202、图在后台执行。要断言"图干了什么"就必须等它跑完。

    ⚠️ 后台任务跑在 **TestClient 自己的事件循环**里，测试进程不能直接 ``await``；
    这里用 TestClient 暴露的 ``portal``（``BlockingPortal``）把 ``drain()``
    投递进那个循环执行。``portal`` 不是星标公开 API —— 把它换掉的代价是整套测试
    改成 async + ``ASGITransport``，对本步来说过重；因此把它**包在这一个函数里**，
    将来要换只改这里。

    422（启动阶段就没通过）不会启动后台图，因此没有东西可等。
    """
    response = test_client.post(
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
    if response.status_code == 202 and settle:
        test_client.portal.call(app.state.background_reviews.drain)
    return response


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
    backend = _Backend()

    response = _post(agent(backend.handler))

    assert response.status_code == 202
    assert response.json() == {"task_id": 33}, "预上传拿到的 review_task_id 原样返回"

    # 1) 确实把文件转发给了 Backend 的接入接口（P14-4 后这是**预上传**那一次）
    #    ⚠️ P9-10 起图尾还会调一次风险写入接口，因此断言的是**第一次**调用
    requests, bodies = backend.requests, backend.bodies
    assert requests[0].method == "POST"
    assert str(requests[0].url) == EXPECTED_UPLOAD_URL
    assert requests[0].headers["content-type"].startswith("multipart/form-data;")
    assert b'name="contract_no"' in bodies[0]
    assert DOCX_BYTES in bodies[0], "文件字节必须原样转发"

    # 2) **图用的就是预上传建出来的那个任务** —— 这是 A 方案成立的证据
    #    图内的 ``upload_file`` 会再上传一次，sha256 幂等让它命中同一个 task；
    #    因此后续写入打的是**同一个** task_id（Backend 侧的真实幂等由 P4 保证）
    assert len(backend.of("/document")) == 1
    assert backend.of("/document")[0].url.path == "/api/v1/review-tasks/33/document"
    assert backend.of("/risks")[0].url.path == "/api/v1/review-tasks/33/risks"

    # 3) 图真的跑到底了，而且**没有**把任务标成阻塞
    backend.assert_not_blocked()


# --------------------------------------------------------------------------- #
# 解析失败 / 空文档 —— workflow_status 必须与 parse_result 一致
# --------------------------------------------------------------------------- #
def test_unparsable_document_is_never_reported_as_completed(
    agent: Callable[[Handler], TestClient],
) -> None:
    """核心不变量：解析失败**绝不能**让任务留在"看起来正常"的中间态。

    这份文件上传能过（Backend 认它是 DOCX），但内容不是合法的 ZIP ——
    解析读不出来。工作流**没有**产出任何可用内容，就必须**如实留下阻塞痕迹**。
    """

    backend = _Backend()

    response = _post(agent(backend.handler), payload=b"this is not a zip archive at all")

    assert response.status_code == 202, "受理与否和「这份能不能审」是两件事"
    # §6.1 早就规定了「解析不可恢复错误 → blocked」，P14-4 才补上写入口。
    # 落在中间态没人管，比报错更糟 —— 所以这里断言的是**阻塞痕迹确实存在**。
    assert backend.block_payloads, "解析失败必须留下阻塞痕迹"
    assert backend.block_payloads[0]["block_reason_code"] == "FILE_CORRUPTED"


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

    backend = _Backend()

    response = _post(agent(backend.handler, rule_sets=rules_handler))

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

    # 4) 图跑到底了：受理 202、写完文档层与风险、**没有**把任务标成阻塞
    assert response.status_code == 202
    backend.assert_not_blocked()
    assert len(backend.of("/document")) == 1
    assert len(backend.of("/risks")) == 1


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

    backend = _Backend()

    # 一份带**真实表格**的 DOCX：付款计划表里「1. 预付款 | <比例>」
    payload = docx_bytes(
        "第一条 付款方式",
        "付款计划如下：",
        [["付款阶段", "比例"], ["1. 预付款", prepay_cell], ["2. 到货款", "60%"]],
    )

    response = _post(agent(backend.handler), payload=payload)

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
    assert response.status_code == 202
    backend.assert_not_blocked()


def test_endpoint_injects_the_llm_provider_from_app_state(
    agent: Callable[[Handler], TestClient],
) -> None:
    """P9-6a：编排层把 provider 从 ``app.state`` 注入 ``ReviewContext``，节点用的是它。

    这里把 provider 换成一个离线替身，**证明注入路径真的通了**
    （换了替身就真的走替身，而不是某个藏在别处的默认实现）。
    """
    provider = _FakeProvider()
    backend = _Backend()

    test_client = agent(backend.handler)  # 先启动（lifespan 会创建默认 provider）
    app.state.llm_provider = provider  # 启动后再替换 —— 与 backend_client 同一手法
    response = _post(test_client)

    assert len(provider.requests) == 1, "llm_review 用的就是注入进来的这个 provider"
    assert response.status_code == 202
    backend.assert_not_blocked()


def test_endpoint_degrades_to_rule_only_when_llm_is_unavailable(
    agent: Callable[[Handler], TestClient],
) -> None:
    """LLM 不可用只是**降级**：这次审查照常完成（规则结果仍可用），不是 422。

    覆盖真实默认路径：lifespan 创建的 ``DeepSeekProvider`` 在本机未配置密钥，
    ``complete()`` 会在本地短路 —— **不会发生任何真实网络调用**。
    """
    if get_settings().llm_configured:
        pytest.skip("本机配置了 DEEPSEEK_*，这条「未配置」用例不适用")

    backend = _Backend()

    response = _post(agent(backend.handler))

    assert response.status_code == 202, "LLM 挂了不该让整次审查失败"
    # 降级不占用失败通道：图照常跑到底，文档层与风险都写进去了，任务**没有**被阻塞
    backend.assert_not_blocked()
    assert len(backend.of("/document")) == 1
    assert len(backend.of("/risks")) == 1


def test_contract_type_without_rule_set_still_completes(
    agent: Callable[[Handler], TestClient],
) -> None:
    """SERVICE：Backend 明确回答"没有启用的规则集"（200 + rule_set=null）= **正常完成**。"""

    backend = _Backend()

    response = _post(
        agent(backend.handler, rule_sets=_json_response(RULE_SETS_SERVICE_PAYLOAD)),
        contract_type="SERVICE",
    )

    assert response.status_code == 202
    backend.assert_not_blocked()
    assert len(backend.of("/document")) == 1


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

    backend = _Backend()

    response = _post(agent(backend.handler), payload=docx_bytes())

    assert response.status_code == 202
    backend.assert_not_blocked()
    assert len(backend.of("/document")) == 1


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
    assert body["error_message"], "启动阶段失败时把原因如实带上"
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
    """无论工作流成败，落盘的临时文件都必须被删除。

    ⚠️ P14-4 后它的生命周期**不再跟着 HTTP 请求**，而是跟着**后台协程** ——
    请求早就返回了，图还在跑。因此"删掉"发生在后台跑完之后（``_post`` 已经等过它）。
    """
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

    assert response.status_code == 202
    assert len(created) == 1, "应当恰好落盘一个临时文件"
    assert not created[0].exists(), "后台跑完后临时文件必须被删除"


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


# --------------------------------------------------------------------------- #
# P14-4：异步启动 / 后台执行 / 失败上报
# --------------------------------------------------------------------------- #
class _FakeGraph:
    """替身图：让"请求到底等没等图"这件事可以被**确定性**地观察到。

    真图在 mock 掉 Backend 之后只要几十毫秒就跑完了，根本来不及观察"请求是否在等"。
    ``gate`` 把图卡住直到测试放行 —— 于是"POST 立刻返回"与"图还在后台跑"能同时断言。

    ⚠️ 它替换的是 ``app.state.review_graph``（与 provider / backend_client 同一手法），
    **不改任何节点**。
    """

    def __init__(self, *, gate: asyncio.Event | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._gate = gate
        self._error = error

    async def ainvoke(self, state: dict[str, Any], *, context: Any = None) -> dict[str, Any]:
        self.calls.append(state)
        if self._gate is not None:
            await self._gate.wait()
        if self._error is not None:
            raise self._error
        return {
            "file_valid": True,
            "error_code": None,
            "parse_result": ParseResult(
                status="PARSED", parser="FakeGraph", source_file_type="DOCX"
            ),
        }


def test_the_request_does_not_wait_for_the_graph(agent: Callable[[Handler], TestClient]) -> None:
    """**P14-4 的核心**：请求在**图还没跑完**时就返回了 202。

    "没等"的证明是**这个用例能跑完** —— gate 一直不放行，如果请求等待图，
    POST 会永远挂住、测试超时。这里再补三条即时可观察的证据。
    """
    backend = _Backend()
    gate = asyncio.Event()
    graph = _FakeGraph(gate=gate)

    test_client = agent(backend.handler)
    app.state.review_graph = graph

    response = _post(test_client, settle=False)  # 故意不等后台

    assert response.status_code == 202
    assert response.json() == {"task_id": 33}
    assert app.state.background_reviews.active_count == 1, "图已经在后台登记并启动"
    assert app.state.background_reviews.active_names() == ["review:33"]

    # 放行并等它跑完，别把任务留给后面的用例
    gate.set()
    test_client.portal.call(app.state.background_reviews.drain)

    assert len(graph.calls) == 1, "图最终跑了一次"
    assert app.state.background_reviews.active_count == 0, "跑完必须从登记处消失"


def test_the_background_run_is_registered_and_then_cleaned_up(
    agent: Callable[[Handler], TestClient],
) -> None:
    """"active task 被记录 → 完成后被清理"这条生命周期在**真实端点**上也成立。

    登记处的单元测试覆盖了各种边界；这里只确认端点确实把它用起来了。
    """
    backend = _Backend()
    test_client = agent(backend.handler)

    _post(test_client)  # 内部已经 drain

    assert app.state.background_reviews.active_count == 0


def test_a_graph_exception_blocks_the_task(agent: Callable[[Handler], TestClient]) -> None:
    """**§七 的核心**：后台图抛异常必须留下痕迹，绝不能静默消失。

    异常发生在**图之外**（``ainvoke`` 自己炸了），因此它的错误码与图内的
    ``error_code`` 分开 —— 排查时看的地方不同。
    """
    backend = _Backend()
    test_client = agent(backend.handler)
    app.state.review_graph = _FakeGraph(error=RuntimeError("图炸了"))

    response = _post(test_client)

    assert response.status_code == 202, "受理过了 —— 失败只能写进任务状态"
    assert backend.block_payloads, "后台异常必须上报为阻塞"
    payload = backend.block_payloads[0]
    assert payload["block_reason_code"] == "AGENT_GRAPH_EXECUTION_FAILED"
    assert "RuntimeError" in payload["block_reason_msg"], "异常类型要进人话原因"


def test_a_background_failure_does_not_leak_a_traceback_to_the_caller(
    agent: Callable[[Handler], TestClient],
) -> None:
    """``block_reason_msg`` 会回到调用方与界面 —— **不许把堆栈塞进去**。"""
    backend = _Backend()
    test_client = agent(backend.handler)
    app.state.review_graph = _FakeGraph(error=RuntimeError("内部细节不应外泄"))

    _post(test_client)

    message = backend.block_payloads[0]["block_reason_msg"]
    assert "Traceback" not in message
    assert "File \"" not in message, "不要带堆栈帧"


class _IdempotentBackend(_Backend):
    """第一次上传是"新建"，第二次起是"复用" —— 与真实 Backend 的 P4 行为一致。

    ⚠️ 真正的"不建第二个任务"是 **Backend** 的保证（``sha256`` UNIQUE +
    ``idempotency_key``），由 P4 的测试与真实 E2E 覆盖。这里钉的是 **Agent 的接线**：
    它返回自己预上传拿到的那个 id，后续写入也打在同一个 task 上。
    """

    def __init__(self) -> None:
        super().__init__()
        self.upload_count = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contracts"):
            self.upload_count += 1
            self.requests.append(request)
            self.bodies.append(await request.aread())
            return httpx.Response(
                200 if self.upload_count > 1 else 201,
                json={**SUCCESS_PAYLOAD, "reused": self.upload_count > 1, "task_reused": self.upload_count > 1},
            )
        return await super().handler(request)


def test_the_pre_upload_and_the_graph_land_on_the_same_task(
    agent: Callable[[Handler], TestClient],
) -> None:
    """**A 方案的核心契约**：预上传建出来的 task，就是图后面用的那个 task。

    有两次 ``POST /contracts``：端点自己那次（预上传）与图内 ``upload_file`` 那次。
    第二次在真实 Backend 上会命中 sha256 幂等、返回**同一个** ``review_task_id`` ——
    假 Backend 在这里照实模拟。于是可以断言：202 给出的 id 与图后续写入的路径
    **指向同一个任务**。
    """
    backend = _IdempotentBackend()
    test_client = agent(backend.handler)

    response = _post(test_client)

    assert response.status_code == 202
    assert response.json() == {"task_id": 33}, "返回的是自己预上传拿到的那个 id"

    assert backend.upload_count == 2, "预上传一次、图内 upload_file 再一次"
    # 图后续所有写入都打在**同一个** task 上 —— 没有第二个任务
    assert backend.of("/document")[0].url.path == "/api/v1/review-tasks/33/document"
    assert backend.of("/risks")[0].url.path == "/api/v1/review-tasks/33/risks"


def test_a_pre_upload_without_a_task_id_is_refused(
    agent: Callable[[Handler], TestClient],
) -> None:
    """预上传 2xx 但响应里没有 ``review_task_id`` → **启动阶段拒绝**，不启动后台图。

    这种响应下 Agent 拿不到 task_id，硬跑下去等于"图在跑、外面不知道它在跑哪个任务"。
    """
    backend = _Backend(payload={k: v for k, v in SUCCESS_PAYLOAD.items() if k != "review_task_id"})
    test_client = agent(backend.handler)

    response = _post(test_client)

    assert response.status_code == 422
    body = response.json()
    assert body["workflow_status"] == "rejected"
    assert body["error_code"] == "BACKEND_CONTRACT_INCOMPLETE"
    assert app.state.background_reviews.active_count == 0, "没拿到 task_id 就不该启动后台图"


# --------------------------------------------------------------------------- #
# P14-4 follow-up：shutdown 取消的**两种**形态，收尾方式必须各管各的
# --------------------------------------------------------------------------- #
def _temp_uploads() -> set[Path]:
    """系统临时目录里 Agent 的落盘文件。断言用**文件系统的事实**，不看日志。"""
    return set(Path(tempfile.gettempdir()).glob("agent-upload-*"))


def _saturate(
    test_client: TestClient, gate: asyncio.Event
) -> BackgroundReviews:
    """把登记处换成"只容得下 1 个、0.05s 就放弃"的实例，并让图卡在闸门上。

    这样第二个 POST 必然**排队**，而 drain 必然超时取消 —— 两种被取消的形态
    因此可以被确定性地制造出来，不依赖真实的时长。

    ⚠️ 只换 ``app.state`` 上的两个对象（与 provider / backend_client 同一手法），
    **不改任何节点、不改端点**。
    """
    registry = BackgroundReviews(concurrency=1, drain_timeout_seconds=0.05)
    app.state.background_reviews = registry
    app.state.review_graph = _FakeGraph(gate=gate)
    return registry


def test_a_queued_review_that_is_abandoned_leaves_no_upload_behind(
    agent: Callable[[Handler], TestClient],
) -> None:
    """**闸门外**那一半：shutdown 时还在排队的审查被放弃，临时文件也必须被删掉。

    ⚠️ 这种任务连图都没开始跑，``_execute_review`` 的 ``finally`` **不会执行**
    （协程一次都没被 ``await`` 过）。删文件的责任因此落在 ``on_abandon`` 上 ——
    漏了它，临时目录里就会留下一份合同副本。
    """
    backend = _Backend()
    gate = asyncio.Event()
    test_client = agent(backend.handler)
    registry = _saturate(test_client, gate)

    before = _temp_uploads()

    first = _post(test_client, settle=False)  # 占住唯一的名额（图卡在 gate 上不放）
    test_client.portal.call(asyncio.sleep, 0.05)
    second = _post(test_client, settle=False)  # 只能排队

    assert first.status_code == second.status_code == 202
    assert registry.active_count == 2
    assert len(_temp_uploads() - before) == 2, "两次请求各自落了一份临时文件"

    test_client.portal.call(registry.drain)

    assert _temp_uploads() - before == set(), "被放弃的排队任务同样必须删掉自己的临时文件"
    assert registry.active_count == 0


def test_a_cancelled_running_review_reports_blocked_then_propagates(
    agent: Callable[[Handler], TestClient],
) -> None:
    """**闸门内**那一半：正在跑的审查被取消 → 如实上报 BLOCKED → 取消继续传播。

    与上一条对照着看：同样是 shutdown 取消，跑起来的那一半由
    ``_execute_review`` 自己的 ``except CancelledError`` 兜住（它要调 Backend，
    所以只能由它做），排队的那一半由 ``on_abandon`` 兜住。两条路**互斥**。

    ⚠️ "取消继续传播"这件事只能这样观察：任务最终处于 ``cancelled``，
    而不是被当成正常结束 —— 吞掉取消会让 asyncio 与登记处都看不出它没跑完。
    """
    backend = _Backend()
    gate = asyncio.Event()
    test_client = agent(backend.handler)
    registry = _saturate(test_client, gate)

    _post(test_client, settle=False)
    test_client.portal.call(asyncio.sleep, 0.05)
    task = next(iter(registry._tasks))

    test_client.portal.call(registry.drain)

    assert backend.block_payloads, "被取消的图必须留下阻塞痕迹（请求早已 202，没人能再返回错误）"
    payload = backend.block_payloads[0]
    assert payload["block_reason_code"] == "AGENT_GRAPH_EXECUTION_FAILED"
    assert "中断" in payload["block_reason_msg"]
    assert task.cancelled(), "上报之后必须让取消继续传播，不能吞掉"
