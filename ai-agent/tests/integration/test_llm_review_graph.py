"""``llm_review`` 在**真实 Graph** 里的接线与分流（P9-6a）。

只 mock **网络层**（``httpx.MockTransport``）与 **LLM provider**（fake），
Agent 自己的节点、State 流转、Conditional Edge 分流全部真实执行：

::

    … → extract_keywords → rule_review → llm_review ─┬─ continue → END
                                                      └─ fallback → END

三条走向各自对应一类结局：

==================================  ============================================
用例                                 期望
==================================  ============================================
LLM 正常                            有 ``llm_findings``，无 ``llm_error_code``
LLM 不可用 / 输出不合契约            进降级通道，**``rule_risks`` 一条不少**
没有 provider                        同样是降级；且**不发任何 LLM 请求**
==================================  ============================================
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.errors import AgentErrorCode
from app.graph.builder import NODE_LLM_REVIEW, NODE_RULE_REVIEW, build_review_graph
from app.graph.context import ReviewContext
from app.llm.json_guard import LLMSchemaInvalidError
from app.llm.provider import LLMProvider, LLMUnavailableError
from app.llm.schemas import LLMRequest, LLMResult
from app.rules.schemas import AgentRule, RuleSetSnapshot
from app.tools.backend_client import BackendClient
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

#: 一份真实 DOCX：IP 条款里写着"知识产权归乙方所有" → 规则会命中
DOCX_PARAGRAPHS = (
    "第一条 知识产权",
    "本项目产生的知识产权归乙方所有。",
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

#: 一份**合法**的模型输出
GOOD_FINDING: dict[str, Any] = {
    "clause_index": 0,
    "risk_title": "知识产权归属供方",
    "risk_level": "HIGH",
    "reason": "成果归属供方会限制我方后续使用。",
    "quote": "知识产权归乙方所有",
    "context_before": "本项目产生的",
    "context_after": "。",
}

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]


class FakeProvider:
    """离线 provider：记录请求，按预设返回或抛出。

    带 ``aclose()`` 是**契约要求**，不是可选项 —— ``LLMProvider`` 的生命周期
    包含它（``lifespan`` 关闭时会调用），漏了就会在关闭阶段炸。
    """

    def __init__(
        self, *, findings: list[dict[str, Any]] | None = None, error: Exception | None = None
    ) -> None:
        self.requests: list[LLMRequest] = []
        self.closed = False
        self._raw = json.dumps({"findings": findings or []}, ensure_ascii=False)
        self._error = error

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return LLMResult(raw_text=self._raw, model="fake-model", provider="fake")

    async def aclose(self) -> None:
        self.closed = True


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


def _ok_handler() -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    return handler


async def _run(backend: BackendClient, source_file: Path, provider: FakeProvider | None) -> dict[str, Any]:
    return await build_review_graph().ainvoke(
        {
            "file_path": str(source_file),
            "filename": "contract.docx",
            "content_type": None,
            "contract_no": "HT-2026-001",
            "title": "设备采购合同",
            "contract_type": "PURCHASE",
            "rule_snapshot": PURCHASE_SNAPSHOT,
        },
        context=ReviewContext(backend=backend, llm=provider),
    )


# --------------------------------------------------------------------------- #
# 替身符合协议
# --------------------------------------------------------------------------- #
def test_double_satisfies_the_provider_protocol() -> None:
    assert isinstance(FakeProvider(), LLMProvider)


# --------------------------------------------------------------------------- #
# 接线
# --------------------------------------------------------------------------- #
def test_llm_review_is_wired_after_rule_review() -> None:
    graph = build_review_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert NODE_LLM_REVIEW in graph.nodes
    assert (NODE_RULE_REVIEW, NODE_LLM_REVIEW) in edges, "接在规则审查之后"


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
async def test_successful_llm_review_writes_findings(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    provider = FakeProvider(findings=[GOOD_FINDING])

    final = await _run(make_backend(_ok_handler()), source_file, provider)

    assert len(final["llm_findings"]) == 1
    assert final["llm_findings"][0].risk_title == "知识产权归属供方"
    assert final.get("llm_error_code") is None
    assert final.get("error_code") is None, "LLM 成功不该产生任何失败信号"
    # 规则结果照旧（两条通道互不影响）
    assert [risk.risk_code for risk in final["rule_risks"]] == ["IP_OWNER_SUPPLIER_001"]


async def test_successful_llm_review_receives_clauses_and_rule_hits(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """模型看到的是**真实文档**切出来的条款，以及规则已命中的提示。"""
    provider = FakeProvider(findings=[])

    await _run(make_backend(_ok_handler()), source_file, provider)

    (request,) = provider.requests
    assert "知识产权归乙方所有" in request.user_prompt, "条款全文进了提示"
    assert "IP_OWNER_SUPPLIER_001" in request.user_prompt, "规则命中带 rule_code 进提示"


async def test_model_findings_are_not_touched_by_the_rule_side(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """``llm_findings`` 是模型的原始发现 —— 不被规则结果污染。"""
    provider = FakeProvider(findings=[GOOD_FINDING])

    final = await _run(make_backend(_ok_handler()), source_file, provider)

    finding = final["llm_findings"][0]
    assert not hasattr(finding, "paragraph_index")
    assert not hasattr(finding, "source")


# --------------------------------------------------------------------------- #
# 降级路径
# --------------------------------------------------------------------------- #
async def test_model_unavailable_degrades_and_keeps_rule_risks(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """**核心要求**：LLM 不可用时，已有的 ``rule_risks`` 一条都不能丢。"""
    provider = FakeProvider(error=LLMUnavailableError("LLM 返回 503", status_code=503))

    final = await _run(make_backend(_ok_handler()), source_file, provider)

    assert final["llm_error_code"] == AgentErrorCode.LLM_UNAVAILABLE.value
    assert "llm_findings" not in final
    assert [risk.risk_code for risk in final["rule_risks"]] == ["IP_OWNER_SUPPLIER_001"]
    assert final.get("error_code") is None, "降级**不是**整次审查的失败"


async def test_schema_invalid_degrades_and_keeps_rule_risks(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    provider = FakeProvider(error=LLMSchemaInvalidError("模型输出不是合法 JSON"))

    final = await _run(make_backend(_ok_handler()), source_file, provider)

    assert final["llm_error_code"] == AgentErrorCode.LLM_SCHEMA_INVALID.value
    assert "llm_findings" not in final
    assert len(final["rule_risks"]) == 1
    assert final.get("error_code") is None


async def test_missing_provider_degrades_without_any_llm_request(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """没有 provider：明确降级，且**不可能**发生 LLM 网络调用（连对象都没有）。"""
    final = await _run(make_backend(_ok_handler()), source_file, None)

    assert final["llm_error_code"] == AgentErrorCode.LLM_UNAVAILABLE.value
    assert "provider" in final["llm_error_message"]
    assert len(final["rule_risks"]) == 1, "规则结果照常"
    assert final["parse_result"].status == "PARSED"


async def test_workflow_still_completes_in_both_branches(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """两条分支都要跑到 END —— fallback 是"绕过 LLM 结果"，不是"半路停下"。"""
    success = await _run(make_backend(_ok_handler()), source_file, FakeProvider(findings=[]))
    degraded = await _run(
        make_backend(_ok_handler()), source_file, FakeProvider(error=LLMUnavailableError("x"))
    )

    for final in (success, degraded):
        assert final["parse_result"].status == "PARSED"
        assert final["clauses"], "两條路都必须把前面的产物带到底"
        assert final["rule_evaluations"], "规则求值在两条路里都完成过"
