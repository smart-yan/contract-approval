"""``persist_document`` 在**真实 Graph** 里的接线与分流（P10-4）。

只 mock **网络层**（``httpx.MockTransport``），Agent 自己的节点全真实执行 ——
因此这里验证的是"图真的把文档层接上了、失败真的会停下来"，而不是
"我 mock 的东西按我想的返回了"：

::

    … → extract_keywords → persist_document ─┬─ 失败 → END
                                              └─ 成功 ↓
                                          rule_review → llm_review
                                          → merge_risks → persist_risks → END

**"rule_review 到底跑没跑"的判据是 ``rule_evaluations``** ——
那是它唯一的产出，比"我断言某个 mock 被调用了几次"更贴近事实。

文档用的是**真实 DOCX**（经「解析 + 条款识别 + 元数据抽取」全链路），
因此这里也顺带验证了 P10-3 的映射在真实数据上成立。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Coroutine
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.errors import AgentErrorCode
from app.graph.builder import NODE_PERSIST_DOCUMENT, build_review_graph
from app.graph.context import ReviewContext
from app.rules.schemas import AgentRule, RuleSetSnapshot
from app.tools.backend_client import BackendClient
from app.tools.document_persistence import DocumentMappingError
from tests.factories import docx_bytes

BACKEND_BASE_URL = "http://backend.test"

SUCCESS_PAYLOAD: dict[str, Any] = {
    "contract_id": 11,
    "contract_no": "HT-2026-001",
    "title": "设备采购合同",
    "contract_type": "PURCHASE",
    "contract_status": "PENDING",
    "file_id": 22,
    "filename": "contract.docx",
    "file_size": 1234,
    "file_type": "DOCX",
    "sha256": "a" * 64,
    "parse_status": "PENDING",
    "review_task_id": 33,
    "task_status": "PENDING",
    "task_stage": "UPLOADED",
    "reused": False,
}

DOCUMENT_PAYLOAD: dict[str, Any] = {
    "task_id": 33,
    "parse_status": "PARSED",
    "blocks_created": 5,
    "blocks_reused": 0,
    "clauses_persisted": 2,
    "metadata_persisted": 1,
    "current_stage": "CLAUSED",
}

RISK_PAYLOAD: dict[str, Any] = {
    "task_id": 33,
    "persisted": 1,
    "task_status": "pending",
    "task_stage": "REVIEWED",
}

DOCX_PARAGRAPHS = (
    "甲方：某某科技有限公司",
    "第一条 知识产权",
    "本项目产生的知识产权归乙方所有。",
    "第二条 违约责任",
    "乙方应在 10 日内完成整改。",
)
DOCX_BYTES = docx_bytes(*DOCX_PARAGRAPHS)

IP_RULE = AgentRule(
    rule_code="IP_OWNER_SUPPLIER_001",
    rule_name="知识产权归属相对方",
    dimension="知识产权",
    rule_type="KEYWORD",
    expression={"keywords": ["知识产权归乙方"], "logic": "ANY"},
    target_clause_types=["IP"],
    severity="HIGH",
    sort_order=10,
)
PURCHASE_SNAPSHOT = RuleSetSnapshot(contract_type="PURCHASE", rule_set_version="v1", rules=[IP_RULE])

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]

_PERSIST_DOCUMENT_MODULE = import_module("app.graph.nodes.persist_document")


class _Router:
    """按路径分发的 Backend 替身 —— 同时记录每个端点被调了几次。"""

    def __init__(
        self,
        *,
        document_status: int = 201,
        document_body: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.document_bodies: list[dict[str, Any]] = []
        self._document_status = document_status
        self._document_body = document_body if document_body is not None else DOCUMENT_PAYLOAD

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/document"):
            self.document_bodies.append(json.loads((await request.aread()).decode()))
            return httpx.Response(self._document_status, json=self._document_body)
        if path.endswith("/risks"):
            return httpx.Response(201, json=RISK_PAYLOAD)
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    def called(self, suffix: str) -> bool:
        return any(path.endswith(suffix) for path in self.calls)


@pytest.fixture
async def make_backend() -> AsyncIterator[Callable[[Handler], BackendClient]]:
    opened: list[httpx.AsyncClient] = []

    def _make(handler: Handler) -> BackendClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opened.append(client)
        return BackendClient(BACKEND_BASE_URL, client=client)

    yield _make

    for client in opened:
        await client.aclose()


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    path = tmp_path / "contract.docx"
    path.write_bytes(DOCX_BYTES)
    return path


async def _run(
    backend: BackendClient, source_file: Path, *, snapshot: Any = PURCHASE_SNAPSHOT
) -> dict[str, Any]:
    return await build_review_graph().ainvoke(
        {
            "file_path": str(source_file),
            "filename": "contract.docx",
            "content_type": None,
            "contract_no": "HT-2026-001",
            "title": "设备采购合同",
            "contract_type": "PURCHASE",
            "rule_snapshot": snapshot,
        },
        context=ReviewContext(backend=backend, llm=None),
    )


# --------------------------------------------------------------------------- #
# 1：正常路径 —— 文档层成功，规则审查照常跑
# --------------------------------------------------------------------------- #
async def test_a_successful_document_layer_lets_the_review_continue(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    router = _Router()

    final = await _run(make_backend(router.handler), source_file)

    assert router.called("/document"), "文档层被调用了"
    assert final.get("error_code") is None
    assert final["rule_evaluations"], "rule_review 真的跑了（它的唯一产出在这里）"
    assert router.called("/risks"), "整条链走到底"


async def test_the_document_payload_comes_from_the_real_document(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """P10-3 的映射在**真实解析产物**上成立：块来自真实段落、坐标能切回原文。"""
    router = _Router()

    final = await _run(make_backend(router.handler), source_file)

    body = router.document_bodies[0]
    paragraphs = final["parse_result"].paragraphs
    assert len(body["blocks"]) == len(paragraphs)
    assert [b["text"] for b in body["blocks"]] == [p.text for p in paragraphs]
    for block in body["blocks"]:
        assert (
            final["parse_result"].text[block["char_start_global"] : block["char_end_global"]] == block["text"]
        )
    # 条款区间是真实切分结果的段落号换算来的
    assert body["clauses"], "真实文档应当切出条款"


async def test_the_document_layer_runs_before_the_rules(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """顺序：文档层先落库，规则审查才跑 —— Backend 的风险接口要求阶段已到 CLAUSED。"""
    router = _Router()

    await _run(make_backend(router.handler), source_file)

    assert router.calls.index("/api/v1/contracts") < next(
        i for i, path in enumerate(router.calls) if path.endswith("/document")
    )


# --------------------------------------------------------------------------- #
# 2：映射失败 → 停，规则审查不跑
# --------------------------------------------------------------------------- #
async def test_a_mapping_error_stops_the_graph(
    make_backend: Callable[[Handler], BackendClient],
    source_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """映射不成立：**异常不冲出 Graph**，翻译成 ``error_code``，规则审查不跑。

    故障注入在 Tool 的映射上 —— 真实文档切出的条款是自洽的，构造不出越界引用，
    而"映射层报错时图必须停下"这条语义必须被验证。

    ⚠️ 补丁打在**节点模块自己的命名空间**上：节点是用
    ``from app.tools.document_persistence import DocumentPersistenceRequest`` 直接引入的，
    改 Tool 模块里的同名属性对它没有影响（本项目在 ``rule_review`` 上踩过同一个坑）。
    """

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise DocumentMappingError("注入的故障：条款引用了不存在的段落号 99")

    monkeypatch.setattr(_PERSIST_DOCUMENT_MODULE, "DocumentPersistenceRequest", _boom)

    router = _Router()
    final = await _run(make_backend(router.handler), source_file)  # 不抛异常

    assert final["error_code"] == AgentErrorCode.DOCUMENT_MAPPING_INVALID.value
    assert "注入的故障" in final["error_message"]
    assert not final.get("rule_evaluations"), "rule_review 不该跑"
    assert not router.called("/risks"), "整条链就该停在这里"


# --------------------------------------------------------------------------- #
# 3：Backend 拒绝 → 停，规则审查不跑
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code", ["DOCUMENT_ALREADY_PERSISTED", "DOCUMENT_BLOCKS_CONFLICT", "PARSE_STATUS_ALREADY_FINAL"]
)
async def test_a_backend_rejection_stops_the_graph(
    code: str, make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    router = _Router(document_status=409, document_body={"code": code, "message": "拒绝"})

    final = await _run(make_backend(router.handler), source_file)

    assert final["error_code"] == code
    assert not final.get("rule_evaluations"), "rule_review 不该跑"
    assert not router.called("/risks")


async def test_an_unreachable_backend_stops_the_graph(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    class _RejectingDocument(_Router):
        async def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/document"):
                raise httpx.ConnectError("connection refused")
            return await super().handler(request)

    final = await _run(make_backend(_RejectingDocument().handler), source_file)

    assert final["error_code"] == AgentErrorCode.BACKEND_UNREACHABLE.value
    assert not final.get("rule_evaluations")


# --------------------------------------------------------------------------- #
# 4：EMPTY —— 照常继续
# --------------------------------------------------------------------------- #
async def test_an_empty_document_continues_the_graph(
    make_backend: Callable[[Handler], BackendClient], tmp_path: Path
) -> None:
    """空文档是**数据问题**（``EMPTY``），不是失败：按 ``PARSED`` + 空 blocks 写回，图继续走。"""
    empty = tmp_path / "empty.docx"
    empty.write_bytes(docx_bytes())
    router = _Router(document_body={**DOCUMENT_PAYLOAD, "blocks_created": 0})

    final = await _run(make_backend(router.handler), empty)

    assert router.called("/document"), "空文档也要写回 —— 否则 parse_status 永远停在 PENDING"
    assert router.document_bodies[0]["parse_status"] == "PARSED"
    assert router.document_bodies[0]["blocks"] == []
    assert final.get("error_code") is None
    assert final["rule_evaluations"], "图继续走到规则审查"


# --------------------------------------------------------------------------- #
# 5：解析失败 → 根本不到文档层
# --------------------------------------------------------------------------- #
async def test_a_failed_parse_never_reaches_the_document_layer(
    make_backend: Callable[[Handler], BackendClient], tmp_path: Path
) -> None:
    """``FAILED`` 走既有的 fail-closed 条件边直接收尾 —— 文档层、规则、LLM 都不该碰。

    ⚠️ 顺带说明一个已知空缺：这也意味着 **Backend 的 ``parse_status=FAILED``
    分支从 Agent 这条路径上永远不可达**（它只对直接调接口的调用方开放）。
    """
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a docx at all")
    router = _Router()

    final = await _run(make_backend(router.handler), broken)

    assert final["error_code"] == AgentErrorCode.PARSE_FAILED.value
    assert not router.called("/document"), "解析失败不该写文档层"
    assert not final.get("rule_evaluations")
    assert not router.called("/risks")


# --------------------------------------------------------------------------- #
# 6：输入只读
# --------------------------------------------------------------------------- #
async def test_the_upstream_artifacts_survive_the_document_layer(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """文档层落库后，``clauses`` / ``metadata`` / ``parse_result`` 原样留在 State 里。"""
    router = _Router()

    final = await _run(make_backend(router.handler), source_file)

    assert final["clauses"], "条款还在"
    assert final["metadata"] is not None
    assert final["parse_result"].status == "PARSED"
    # 重跑一次不会因为"上次落过库"而改动这些产物
    again = await _run(make_backend(router.handler), source_file)
    assert [c.clause_index for c in again["clauses"]] == [c.clause_index for c in final["clauses"]]


def test_the_document_node_is_registered() -> None:
    graph = build_review_graph().get_graph()

    assert NODE_PERSIST_DOCUMENT in graph.nodes
