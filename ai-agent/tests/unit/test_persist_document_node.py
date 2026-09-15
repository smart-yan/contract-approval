"""``persist_document`` 节点与它的条件边（P10-4）。

节点只做 State 编解码，因此这里测三件事：

1. 它**确实**把三段产物交给了 Tool（而不是"看起来跑了"）
2. 失败的两条路径（映射不成立 / Backend 拒绝）都**翻译成 State**，
   异常绝不冲出 Graph
3. 路由与拓扑：成功才进 ``rule_review``

字段映射与坐标换算由 ``tests/unit/test_document_persistence_tool.py`` 负责，这里不重复。
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
from langgraph.runtime import Runtime

from app.core.errors import AgentErrorCode
from app.graph.builder import (
    NODE_EXTRACT_KEYWORDS,
    NODE_LLM_REVIEW,
    NODE_MERGE_RISKS,
    NODE_PERSIST_DOCUMENT,
    NODE_PERSIST_RISKS,
    NODE_RULE_REVIEW,
    build_review_graph,
)
from app.graph.context import ReviewContext
from app.graph.edges.routing import route_after_persist_document
from app.graph.nodes.persist_document import persist_document
from app.graph.state import ContractReviewState
from app.schemas.document import Paragraph, ParseResult
from app.schemas.understanding import Clause, MetadataItem
from app.tools.backend_client import BackendClient

BACKEND_BASE_URL = "http://backend.test"
EXPECTED_URL = f"{BACKEND_BASE_URL}/api/v1/review-tasks/33/document"

#: ⚠️ 包命名空间里 ``app.graph.nodes.persist_document`` 指的是**函数**。读源码要拿模块对象。
_PERSIST_DOCUMENT_MODULE = import_module("app.graph.nodes.persist_document")

_TEXTS = ("第一条 知识产权", "本项目产生的知识产权归乙方所有。")


def _parse_result(**overrides: Any) -> ParseResult:
    payload: dict[str, Any] = {
        "status": "PARSED",
        "parser": "DocxParser",
        "source_file_type": "DOCX",
        "text": "\n".join(_TEXTS),
        "paragraphs": [
            Paragraph(index=i, block_type="PARAGRAPH", text=text) for i, text in enumerate(_TEXTS)
        ],
    }
    payload.update(overrides)
    return ParseResult(**payload)


def _clause() -> Clause:
    return Clause(
        clause_index=0,
        clause_no="第一条",
        title="知识产权",
        clause_type="IP",
        start_paragraph_index=0,
        end_paragraph_index=1,
        text="\n".join(_TEXTS),
        extract_method="RULE",
    )


def _metadata() -> MetadataItem:
    return MetadataItem(
        field_key="counterparty_name",
        field_label="相对方名称",
        field_value="乙方",
        value_type="TEXT",
        paragraph_index=1,
        quote=_TEXTS[1],
        extract_method="REGEX",
    )


def _state(**overrides: Any) -> ContractReviewState:
    payload: dict[str, Any] = {
        "review_task_id": 33,
        "file_id": 22,
        "parse_result": _parse_result(),
        "clauses": [_clause()],
        "metadata": [_metadata()],
    }
    payload.update(overrides)
    return ContractReviewState(**payload)


class _RecordingBackend:
    """记录调用并按预设返回的 Backend 替身（用真实 ``BackendClient`` + MockTransport）。"""

    def __init__(self, *, status_code: int = 201, body: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self._status_code = status_code
        self._body = (
            body
            if body is not None
            else {
                "task_id": 33,
                "parse_status": "PARSED",
                "blocks_created": 2,
                "blocks_reused": 0,
                "clauses_persisted": 1,
                "metadata_persisted": 1,
                "current_stage": "CLAUSED",
            }
        )

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads((await request.aread()).decode()))
        return httpx.Response(self._status_code, json=self._body)

    def client(self) -> BackendClient:
        return BackendClient(
            BACKEND_BASE_URL, client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        )


async def _run(state: ContractReviewState, backend: _RecordingBackend) -> dict[str, object]:
    return await persist_document(state, Runtime(context=ReviewContext(backend=backend.client())))


# --------------------------------------------------------------------------- #
# 成功
# --------------------------------------------------------------------------- #
async def test_it_sends_the_three_artifacts_in_one_request() -> None:
    backend = _RecordingBackend()

    result = await _run(_state(), backend)

    assert result == {}, "成功时没有需要新写进 State 的事实"
    assert [str(r.url) for r in backend.requests] == [EXPECTED_URL]
    body = backend.bodies[0]
    assert body["parse_status"] == "PARSED"
    assert len(body["blocks"]) == 2
    assert body["clauses"][0]["start_block_index"] == 0
    assert body["metadata"][0]["source_block_index"] == 1


async def test_an_empty_document_still_goes_out_as_parsed() -> None:
    """``EMPTY``（解析成功但正文为空）→ ``PARSED`` + 空 blocks。

    它是**数据问题**不是失败 —— Backend 侧用"零个块"表达，任务阶段照常推进，
    因此图要继续往下走，而不是把它当成解析失败。
    """
    backend = _RecordingBackend(body={"task_id": 33, "parse_status": "PARSED", "blocks_created": 0})
    state = _state(
        parse_result=_parse_result(status="EMPTY", text="", paragraphs=[]),
        clauses=[],
        metadata=[],
    )

    result = await _run(state, backend)

    assert result == {}
    assert backend.bodies[0]["parse_status"] == "PARSED"
    assert backend.bodies[0]["blocks"] == []


# --------------------------------------------------------------------------- #
# 失败一：映射不成立
# --------------------------------------------------------------------------- #
async def test_a_mapping_error_becomes_a_state_result_and_never_escapes() -> None:
    """**异常绝不允许冲出 Graph**。

    条款引用一个不存在的段落号 → ``DocumentMappingError``。节点必须把它翻译成
    State 里的业务失败，而不是让 500 冒到调用方那里 —— 那样一次"说得清的失败"
    就变成了一页堆栈。
    """
    backend = _RecordingBackend()
    broken = Clause(
        clause_index=0,
        clause_no=None,
        title=None,
        clause_type="IP",
        start_paragraph_index=0,
        end_paragraph_index=9,  # 越界：本次只解析出 2 段
        text="x",
        extract_method="RULE",
    )

    result = await _run(_state(clauses=[broken]), backend)  # 不抛异常

    assert result["error_code"] == AgentErrorCode.DOCUMENT_MAPPING_INVALID.value
    assert "不存在的段落号 9" in str(result["error_message"])
    assert backend.requests == [], "映射都没成立，不该发出请求"


async def test_the_mapping_error_message_says_which_artifact_is_broken() -> None:
    backend = _RecordingBackend()
    broken = MetadataItem(
        field_key="contract_amount",
        field_label="合同金额",
        field_value="100",
        value_type="AMOUNT",
        paragraph_index=77,
        quote="x",
        extract_method="REGEX",
    )

    result = await _run(_state(metadata=[broken]), backend)

    assert result["error_code"] == AgentErrorCode.DOCUMENT_MAPPING_INVALID.value
    assert "contract_amount" in str(result["error_message"])


# --------------------------------------------------------------------------- #
# 失败二：Backend 拒绝 / 连不上
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code", ["DOCUMENT_ALREADY_PERSISTED", "DOCUMENT_BLOCKS_CONFLICT", "PARSE_STATUS_ALREADY_FINAL"]
)
async def test_a_backend_rejection_is_reported_verbatim(code: str) -> None:
    backend = _RecordingBackend(status_code=409, body={"code": code, "message": "拒绝"})

    result = await _run(_state(), backend)

    assert result["error_code"] == code, "Backend 的错误码原样带回，不翻译"
    assert "llm_error_code" not in result, "**不是**降级通道"


async def test_an_unreachable_backend_is_reported() -> None:
    class _Unreachable(_RecordingBackend):
        async def handler(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

    result = await _run(_state(), _Unreachable())

    assert result["error_code"] == AgentErrorCode.BACKEND_UNREACHABLE.value


# --------------------------------------------------------------------------- #
# 输入缺失
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("missing", ["review_task_id", "parse_result"])
async def test_a_missing_input_is_an_input_error(missing: str) -> None:
    backend = _RecordingBackend()

    result = await _run(_state(**{missing: None}), backend)

    assert result["error_code"] == AgentErrorCode.AGENT_INPUT_INVALID.value
    assert missing in str(result["error_message"])
    assert backend.requests == [], "输入不齐备时不该发出任何请求"


# --------------------------------------------------------------------------- #
# 只读
# --------------------------------------------------------------------------- #
async def test_the_input_state_is_not_modified() -> None:
    """``clauses`` / ``metadata`` / ``risks`` 原样留在 State 里 —— 它们是这次审查的证据。"""
    backend = _RecordingBackend()
    clauses = [_clause()]
    metadata = [_metadata()]
    risks: list[object] = [object()]
    state = _state(clauses=clauses, metadata=metadata, risks=risks)
    before = (
        json.dumps(clauses[0].model_dump(), ensure_ascii=False),
        json.dumps(metadata[0].model_dump(), ensure_ascii=False),
    )

    await _run(state, backend)

    after = (
        json.dumps(clauses[0].model_dump(), ensure_ascii=False),
        json.dumps(metadata[0].model_dump(), ensure_ascii=False),
    )
    assert after == before
    assert state["clauses"] is clauses
    assert state["metadata"] is metadata
    assert state["risks"] is risks


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", ["DOCUMENT_MAPPING_INVALID", "DOCUMENT_ALREADY_PERSISTED", "X"])
def test_the_route_stops_on_any_error_code(code: str) -> None:
    """只要 ``error_code`` 有值就停 —— 文档层失败是致命的，继续跑只是白烧模型调用。"""
    assert route_after_persist_document({"error_code": code}) == "stop"


def test_the_route_continues_without_an_error() -> None:
    assert route_after_persist_document({}) == "continue"
    assert route_after_persist_document({"error_code": None}) == "continue"


# --------------------------------------------------------------------------- #
# 拓扑
# --------------------------------------------------------------------------- #
def test_the_document_layer_sits_between_keywords_and_rules() -> None:
    """顺序：``extract_keywords → persist_document → rule_review → llm_review
    → merge_risks → persist_risks``。

    ⚠️ 文档层必须在规则审查**之前**：风险依赖条款，而 Backend 的风险接口要求
    任务阶段已到 ``CLAUSED``。写不回去就继续跑，整条链的产出最终都落不了库。
    """
    graph = build_review_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert (NODE_EXTRACT_KEYWORDS, NODE_PERSIST_DOCUMENT) in edges
    assert (NODE_PERSIST_DOCUMENT, NODE_RULE_REVIEW) in edges
    assert (NODE_PERSIST_DOCUMENT, END) in edges, "失败那一侧直接收尾"
    assert (NODE_RULE_REVIEW, NODE_LLM_REVIEW) in edges
    assert (NODE_LLM_REVIEW, NODE_MERGE_RISKS) in edges
    assert (NODE_MERGE_RISKS, NODE_PERSIST_RISKS) in edges
    assert (NODE_PERSIST_RISKS, END) in edges

    assert (NODE_EXTRACT_KEYWORDS, NODE_RULE_REVIEW) not in edges, "规则审查不能再直连关键词"


def test_the_document_node_has_exactly_one_entry() -> None:
    graph = build_review_graph().get_graph()

    incoming = {edge.source for edge in graph.edges if edge.target == NODE_PERSIST_DOCUMENT}

    assert incoming == {NODE_EXTRACT_KEYWORDS}


def test_the_document_branch_table_points_at_the_two_real_targets() -> None:
    from app.graph.builder import DOCUMENT_ROUTES

    assert DOCUMENT_ROUTES == {"continue": NODE_RULE_REVIEW, "stop": END}


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #
def test_the_node_does_not_touch_the_database_or_the_llm() -> None:
    """依赖集合被钉死：只经 Backend HTTP 写入，不认识 SQLAlchemy / MySQL / LLM。"""
    tree = ast.parse(inspect.getsource(_PERSIST_DOCUMENT_MODULE))
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
        "app.tools.document_persistence",
        "langgraph.runtime",
        "logging",
    }
    assert not any("sqlalchemy" in name or "app.llm" in name for name in modules)
