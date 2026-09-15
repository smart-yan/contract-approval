"""风险持久化的边界层（P9-10）。

这一层最该被钉死的事情只有一件：**字段映射**。
两个模型里有同名字段（``original_text``）**语义正好相反**，所以本文件的
核心用例不是"字段搬过去了"，而是"**没有搬错**"。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from app.core.constants import RiskSource
from app.core.errors import AgentErrorCode
from app.risk.schemas import AgentRiskItem
from app.tools.backend_client import BackendClient
from app.tools.risk_persistence import (
    RiskPersistenceRequest,
    RiskPersistenceTool,
)

BACKEND_BASE_URL = "http://backend.test"
EXPECTED_URL = f"{BACKEND_BASE_URL}/api/v1/review-tasks/33/risks"

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]

#: 段落原文 —— 它**不该**出现在请求里（Backend 的 ``original_text`` 列要的是 quote）
_PARAGRAPH_TEXT = "本项目产生的知识产权归乙方所有，乙方无需另行授权。"


def _risk(**overrides: object) -> AgentRiskItem:
    payload: dict[str, Any] = {
        "source": RiskSource.RULE_AND_LLM,
        "risk_code": "IP_OWNER_SUPPLIER_001",
        "risk_title": "知识产权归属相对方",
        "dimension": "知识产权",
        "risk_level": "HIGH",
        "reason": "命中规则关键词，且模型复核确认。",
        "legal_basis": "《民法典》第 843 条",
        "original_text": _PARAGRAPH_TEXT,
        "quote": "知识产权归乙方所有",
        "paragraph_index": 23,
        "anchor_method": "CLAUSE_SCOPED",
        "related_rule_code": "IP_OWNER_SUPPLIER_001",
    }
    payload.update(overrides)
    return AgentRiskItem(**payload)


@pytest.fixture
async def make_backend() -> Any:
    opened: list[httpx.AsyncClient] = []

    def _make(handler: Handler) -> BackendClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opened.append(client)
        return BackendClient(BACKEND_BASE_URL, client=client)

    yield _make

    for client in opened:
        await client.aclose()


def _ok_handler(captured: list[dict[str, Any]] | None = None) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(json.loads((await request.aread()).decode()))
        return httpx.Response(
            201,
            json={"task_id": 33, "persisted": 1, "task_status": "pending", "task_stage": "REVIEWED"},
        )

    return handler


# --------------------------------------------------------------------------- #
# 字段映射
# --------------------------------------------------------------------------- #
async def test_every_business_field_is_mapped(make_backend: Any) -> None:
    captured: list[dict[str, Any]] = []

    result = await RiskPersistenceTool(make_backend(_ok_handler(captured))).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk()])
    )

    assert result.ok is True
    (item,) = captured[0]["risks"]
    assert item == {
        "risk_code": "IP_OWNER_SUPPLIER_001",
        "risk_title": "知识产权归属相对方",
        "dimension": "知识产权",
        "risk_level": "HIGH",
        "source": "RULE+LLM",
        "reason": "命中规则关键词，且模型复核确认。",
        "legal_basis": "《民法典》第 843 条",
        "quote": "知识产权归乙方所有",
        "paragraph_index": 23,
        "anchor_method": "CLAUSE_SCOPED",
    }


async def test_the_paragraph_text_never_reaches_the_request(make_backend: Any) -> None:
    """⚠️ 本模块存在的首要理由。

    ``AgentRiskItem.original_text`` 是**证据所在的段落原文**，而 Backend 的
    ``risk_item.original_text`` 是**命中的原文片段**。字段同名、语义相反 ——
    一旦有人"顺手"按名字对拷，人工核对时看到的就会是整段文字而不是精确证据。

    这条用例从**请求体全文**上断言：段落原文一个字都没出现过。
    """
    captured: list[dict[str, Any]] = []

    await RiskPersistenceTool(make_backend(_ok_handler(captured))).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk()])
    )

    body = json.dumps(captured[0], ensure_ascii=False)
    assert _PARAGRAPH_TEXT not in body, "段落原文不该出现在任何字段里"
    assert captured[0]["risks"][0]["quote"] == "知识产权归乙方所有", "要传的是 quote"


async def test_agent_only_fields_are_not_sent(make_backend: Any) -> None:
    """``related_rule_code`` 在 ``risk_item`` 里没有列（P9-10 裁决接受丢弃）。

    它的使命在合并时就结束了；把它塞进请求只会让 Backend 收到一个不认识的键。
    """
    captured: list[dict[str, Any]] = []

    await RiskPersistenceTool(make_backend(_ok_handler(captured))).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk()])
    )

    (item,) = captured[0]["risks"]
    assert "related_rule_code" not in item
    assert "original_text" not in item


@pytest.mark.parametrize(
    "forbidden",
    ["review_status", "locator_type", "rule_id", "clause_id", "task_id", "contract_id", "id"],
)
async def test_server_side_fields_are_not_sent(forbidden: str, make_backend: Any) -> None:
    """这些字段**由 Backend 决定**，Agent 传了也不该存在于请求里。

    （``review_status`` 尤其重要：客户端能指定它就等于能伪造"已确认"。）
    """
    captured: list[dict[str, Any]] = []

    await RiskPersistenceTool(make_backend(_ok_handler(captured))).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk()])
    )

    assert forbidden not in captured[0]["risks"][0]


# --------------------------------------------------------------------------- #
# 调用
# --------------------------------------------------------------------------- #
async def test_it_posts_the_whole_batch_to_the_task_scoped_endpoint(make_backend: Any) -> None:
    urls: list[str] = []
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        methods.append(request.method)
        return httpx.Response(201, json={"task_id": 33, "persisted": 2})

    await RiskPersistenceTool(make_backend(handler)).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk(), _risk(risk_code=None, source=RiskSource.LLM)])
    )

    assert urls == [EXPECTED_URL], "任务是路径的一部分，不再往请求体里塞一遍 task_id"
    assert methods == ["POST"]


async def test_an_empty_batch_is_still_sent(make_backend: Any) -> None:
    """``risks=[]`` 是一次**真实结论**（这次审查没有风险），不是"不用调"。

    跳过这次调用会让任务永远停在 pending，而"没有风险"与"我们没跑"就再也分不开了。
    """
    captured: list[dict[str, Any]] = []

    result = await RiskPersistenceTool(make_backend(_ok_handler(captured))).run(
        RiskPersistenceRequest(task_id=33, risks=[])
    )

    assert captured[0]["risks"] == []
    assert result.ok is True


async def test_the_response_is_read_defensively(make_backend: Any) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"task_id": 33, "persisted": 3, "task_status": "pending", "task_stage": "REVIEWED"},
        )

    result = await RiskPersistenceTool(make_backend(handler)).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk()])
    )

    # ``task_status`` 是 Backend 的当前事实，Agent **原样读取、不做解释** ——
    # 状态机怎么推进由 Backend 决定（P9-10 只推进到 current_stage）
    assert (result.persisted, result.task_status, result.task_stage) == (3, "pending", "REVIEWED")


# --------------------------------------------------------------------------- #
# 失败
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", ["TASK_ALREADY_PERSISTED", "RULE_NOT_FOUND", "VALIDATION_ERROR"])
async def test_a_backend_rejection_is_reported_verbatim(code: str, make_backend: Any) -> None:
    """Backend 的错误码**原样带回**，不翻译成 Agent 自己的码。

    翻译会丢掉"到底是谁拒绝的、为什么"这一层信息 ——
    而 ``TASK_ALREADY_PERSISTED`` 与"连不上 Backend"是两种完全不同的处置。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": code, "message": "拒绝"})

    result = await RiskPersistenceTool(make_backend(handler)).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk()])
    )

    assert result.ok is False
    assert result.error_code == code
    assert result.error_message == "拒绝"
    assert result.persisted is None


async def test_an_unreachable_backend_is_reported(make_backend: Any) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = await RiskPersistenceTool(make_backend(handler)).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk()])
    )

    assert result.ok is False
    assert result.error_code == AgentErrorCode.BACKEND_UNREACHABLE.value


async def test_a_non_json_success_body_is_reported(make_backend: Any) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"<html>ok</html>")

    result = await RiskPersistenceTool(make_backend(handler)).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk()])
    )

    assert result.ok is False
    assert result.error_code == AgentErrorCode.BACKEND_REJECTED.value


async def test_an_error_body_that_is_not_the_standard_shape_is_reported(make_backend: Any) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>boom</html>")

    result = await RiskPersistenceTool(make_backend(handler)).run(
        RiskPersistenceRequest(task_id=33, risks=[_risk()])
    )

    assert result.ok is False
    assert result.error_code == AgentErrorCode.BACKEND_REJECTED.value
