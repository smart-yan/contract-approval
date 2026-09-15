"""``persist_risks`` 节点（P9-10）。

节点只做 State 编解码，因此这里测三件事：

1. 它**确实**把 ``state["risks"]`` 交给了 Tool（而不是"看起来跑了"）
2. 失败走的是**致命通道**（``error_code``），不是 LLM 那种降级通道
3. 输入只读、图接线正确

字段映射由 ``tests/unit/test_risk_persistence_tool.py`` 负责，这里不重复。
"""

from __future__ import annotations

import ast
import inspect
import json
from importlib import import_module
from typing import Any

import httpx
import pytest
from langgraph.graph import END

from app.core.constants import RiskSource
from app.core.errors import AgentErrorCode
from app.graph.builder import (
    NODE_MERGE_RISKS,
    NODE_PERSIST_RISKS,
    build_review_graph,
)
from app.graph.context import ReviewContext
from app.graph.nodes.persist_risks import persist_risks
from app.graph.state import ContractReviewState
from app.risk.schemas import AgentRiskItem
from app.tools.backend_client import BackendClient

BACKEND_BASE_URL = "http://backend.test"
EXPECTED_URL = f"{BACKEND_BASE_URL}/api/v1/review-tasks/33/risks"

#: ⚠️ 包命名空间里 ``app.graph.nodes.persist_risks`` 指的是**函数**（``nodes/__init__.py``
#: 重导出了它）。要读源码必须拿模块对象。
_PERSIST_RISKS_MODULE = import_module("app.graph.nodes.persist_risks")


def _risk(**overrides: object) -> AgentRiskItem:
    payload: dict[str, Any] = {
        "source": RiskSource.RULE,
        "risk_code": "IP_OWNER_SUPPLIER_001",
        "risk_title": "知识产权归属相对方",
        "dimension": "知识产权",
        "risk_level": "HIGH",
        "reason": "命中规则关键词。",
        "legal_basis": None,
        "original_text": "本项目产生的知识产权归乙方所有。",
        "quote": "知识产权归乙方",
        "paragraph_index": 23,
        "anchor_method": None,
        "related_rule_code": None,
    }
    payload.update(overrides)
    return AgentRiskItem(**payload)


def _state(**overrides: object) -> ContractReviewState:
    payload: dict[str, Any] = {"review_task_id": 33, "risks": [_risk()], "file_id": 22}
    payload.update(overrides)
    return ContractReviewState(**payload)


class _RecordingBackend:
    """记录调用并按预设返回的 Backend 替身。

    ⚠️ 用 ``BackendClient`` + ``MockTransport`` 而不是 duck-typing 的假对象：
    节点拿到的是 ``runtime.context.backend``，真实类型是 ``BackendClient``；
    换成一个"长得像"的对象会让"它到底调没调对方法"测不出来。
    """

    def __init__(self, *, status_code: int = 201, body: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self._status_code = status_code
        self._body = (
            body
            if body is not None
            else {
                "task_id": 33,
                "persisted": 1,
                "task_status": "pending",
                "task_stage": "REVIEWED",
            }
        )

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads((await request.aread()).decode()))
        return httpx.Response(self._status_code, json=self._body)

    def client(self) -> BackendClient:
        return BackendClient(
            BACKEND_BASE_URL,
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
        )


async def _run(state: ContractReviewState, backend: _RecordingBackend) -> dict[str, object]:
    from langgraph.runtime import Runtime

    return await persist_risks(state, Runtime(context=ReviewContext(backend=backend.client())))


# --------------------------------------------------------------------------- #
# 成功
# --------------------------------------------------------------------------- #
async def test_it_sends_the_merged_risks_to_the_backend() -> None:
    backend = _RecordingBackend()

    result = await _run(_state(), backend)

    assert result == {}, "成功时没有需要新写进 State 的事实"
    assert [str(r.url) for r in backend.requests] == [EXPECTED_URL]
    assert backend.bodies[0]["risks"][0]["quote"] == "知识产权归乙方"


async def test_a_degraded_llm_run_can_still_persist_rule_only_risks() -> None:
    """**LLM 降级不影响持久化**：``llm_findings`` 缺失时，规则风险照常写回。

    这是"降级为仅规则结果"这条链路的最后一环 —— 前面都跑通了、
    最后一步写不进去的话，规则结果同样等于没产出。
    """
    backend = _RecordingBackend()
    state = _state(rule_risks=[], llm_error_code="LLM_UNAVAILABLE")

    result = await _run(state, backend)

    assert result == {}
    assert backend.bodies[0]["risks"][0]["source"] == "RULE"


async def test_an_empty_risk_list_is_still_persisted() -> None:
    """没有风险也要写：那是一次**真实结论**，Backend 据此把任务置为已完成。"""
    backend = _RecordingBackend(body={"task_id": 33, "persisted": 0})

    result = await _run(_state(risks=[]), backend)

    assert result == {}
    assert backend.bodies[0]["risks"] == []


# --------------------------------------------------------------------------- #
# 失败：致命通道
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status_code, code",
    [
        (409, "TASK_ALREADY_PERSISTED"),
        (404, "RULE_NOT_FOUND"),
        (422, "VALIDATION_ERROR"),
    ],
)
async def test_a_backend_rejection_fails_the_run(status_code: int, code: str) -> None:
    """写不进去就是**整次审查失败**（``error_code``），不是降级。

    State 不落库 —— 风险没写进 Backend，等于这次审查什么都没产出。
    若走 LLM 那种降级通道，调用方会拿到一个看起来成功的响应。
    """
    backend = _RecordingBackend(status_code=status_code, body={"code": code, "message": "拒绝"})

    result = await _run(_state(), backend)

    assert result["error_code"] == code
    assert "llm_error_code" not in result, "**不是**第二条降级通道"


async def test_an_unreachable_backend_fails_the_run() -> None:
    class _Unreachable(_RecordingBackend):
        async def handler(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

    result = await _run(_state(), _Unreachable())

    assert result["error_code"] == AgentErrorCode.BACKEND_UNREACHABLE.value


async def test_a_missing_task_id_is_an_input_error() -> None:
    """没有 ``review_task_id`` 就无从写入 —— 是编排异常，**不该发请求**。"""
    backend = _RecordingBackend()

    result = await _run(_state(review_task_id=None), backend)

    assert result["error_code"] == AgentErrorCode.AGENT_INPUT_INVALID.value
    assert backend.requests == [], "输入不齐备时不该发出任何请求"


# --------------------------------------------------------------------------- #
# 只读
# --------------------------------------------------------------------------- #
async def test_the_state_is_not_modified() -> None:
    """``risks`` 原样留在 State 里 —— 它既是写回的内容，也是这次审查的产出记录。"""
    backend = _RecordingBackend()
    risks = [_risk(), _risk(risk_code="LIAB_UNLIMITED_001", dimension="违约责任")]
    state = _state(risks=risks)
    before = [r.model_dump() for r in risks]

    await _run(state, backend)

    assert [r.model_dump() for r in risks] == before
    assert state["risks"] is risks


# --------------------------------------------------------------------------- #
# 接线与依赖
# --------------------------------------------------------------------------- #
def test_the_graph_ends_with_the_persist_node() -> None:
    """顺序：``merge_risks → persist_risks → END``。"""
    graph = build_review_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert NODE_PERSIST_RISKS in graph.nodes
    assert (NODE_MERGE_RISKS, NODE_PERSIST_RISKS) in edges, "合并之后才谈落库"
    assert (NODE_PERSIST_RISKS, END) in edges, "落库是最后一步"

    incoming = {edge.source for edge in graph.edges if edge.target == NODE_PERSIST_RISKS}
    assert incoming == {NODE_MERGE_RISKS}, "落库只有一个入口"


def test_the_persist_node_has_no_conditional_branch_of_its_own() -> None:
    """落库没有分支：成功了继续收尾，失败了写 error_code —— 两条都走到 END。"""
    graph = build_review_graph().get_graph()

    outgoing = [edge for edge in graph.edges if edge.source == NODE_PERSIST_RISKS]

    assert len(outgoing) == 1
    assert not outgoing[0].conditional


def test_the_node_does_not_touch_the_database_or_the_llm() -> None:
    """依赖集合被钉死：只经 Backend HTTP 写入，不认识 SQLAlchemy / MySQL。

    Agent 直连数据库会绕过 Backend 的幂等与事务边界 —— 那是架构红线。
    """
    tree = ast.parse(inspect.getsource(_PERSIST_RISKS_MODULE))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert modules == {
        "__future__",
        "app.core.errors",
        "app.graph.context",
        "app.graph.state",
        "app.tools.risk_persistence",
        "langgraph.runtime",
        "logging",
    }
    assert not any("sqlalchemy" in name or "app.llm" in name for name in modules)
