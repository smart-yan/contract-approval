"""``llm_review`` 在**真实 Graph** 里的接线与分流（P9-6a）。

只 mock **网络层**（``httpx.MockTransport``）与 **LLM provider**（fake），
Agent 自己的节点、State 流转、Conditional Edge 分流全部真实执行：

::

    … → extract_keywords → rule_review → llm_review ─┬─ continue ─┐
                                                      └─ fallback ─┴→ merge_risks → END

三条走向各自对应一类结局：

==================================  ============================================
用例                                 期望
==================================  ============================================
LLM 正常                            有 ``llm_findings``，无 ``llm_error_code``
LLM 不可用 / 输出不合契约            进降级通道，**``rule_risks`` 一条不少**
没有 provider                        同样是降级；且**不发任何 LLM 请求**
==================================  ============================================

**两条分支都通向 ``merge_risks``**（P9-9 接入）：降级的意思是"这次没有模型结论"，
不是"风险合并不用做了" —— 所以无论走哪条路，最后都有一份统一风险列表。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.constants import RiskSource
from app.core.errors import AgentErrorCode
from app.graph.builder import NODE_LLM_REVIEW, NODE_RULE_REVIEW, build_review_graph
from app.graph.context import ReviewContext
from app.llm.finding_resolution import ResolvedFinding
from app.llm.json_guard import LLMSchemaInvalidError
from app.llm.provider import LLMProvider, LLMUnavailableError
from app.llm.schemas import LLMRequest, LLMResult
from app.risk.schemas import AgentRiskItem
from app.rules.schemas import AgentRule, RuleRisk, RuleSetSnapshot
from app.tools.backend_client import BackendClient
from app.understanding.locator import ANCHOR_CLAUSE_SCOPED
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
    "dimension": "知识产权",
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
    (resolved,) = final["llm_findings"]
    assert resolved.finding.risk_title == "知识产权归属供方"
    assert resolved.paragraph_index == 1, "P9-7：真实文档里定位到第 1 段"
    assert resolved.anchor_method == ANCHOR_CLAUSE_SCOPED
    assert resolved.quote in resolved.original_text
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
    """``llm_findings`` 带着**定位**（P9-7），但**仍然不是风险项**。

    分工要看得清：``ResolvedFinding`` 有段落号（Agent 核出来的），
    而里面的 ``LLMFinding`` 只有模型说的话 —— 没有段落号、没有 ``source``、
    没有风险等级以外的加工。合并与统一风险模型是后续步骤的事。
    """
    provider = FakeProvider(findings=[GOOD_FINDING])

    final = await _run(make_backend(_ok_handler()), source_file, provider)

    resolved = final["llm_findings"][0]
    assert hasattr(resolved, "paragraph_index"), "P9-7：定位结果在这一层"
    assert not hasattr(resolved.finding, "paragraph_index"), "模型不产出坐标系"
    assert not hasattr(resolved.finding, "source")


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


# --------------------------------------------------------------------------- #
# 统一风险（P9-9 接过 llm_review 之后的收尾节点）
# --------------------------------------------------------------------------- #
#: 与规则**同名同段**的模型结论：带着 `related_rule_code`，会被合并成一条 RULE+LLM
MATCHING_FINDING: dict[str, Any] = {**GOOD_FINDING, "related_rule_code": "IP_OWNER_SUPPLIER_001"}


async def test_the_run_finishes_with_a_unified_risk_list(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """跑到 END 时，State 里有一份**统一形状**的风险列表（不再是两份各说各话）。

    ``GOOD_FINDING`` 没有 ``related_rule_code``，因此规则那条与模型那条**不合并** ——
    两条来源各自成一条，但都是同一种类型（``AgentRiskItem``）。
    """
    final = await _run(make_backend(_ok_handler()), source_file, FakeProvider(findings=[GOOD_FINDING]))

    risks = final["risks"]
    assert [risk.source for risk in risks] == [RiskSource.RULE, RiskSource.LLM]
    assert {type(risk) for risk in risks} == {AgentRiskItem}
    assert all(risk.quote in risk.original_text for risk in risks)


async def test_a_matching_finding_is_merged_with_its_rule(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """模型说"我就是规则 IP_OWNER_SUPPLIER_001"且落在同一段 → 合成**一条**。"""
    final = await _run(make_backend(_ok_handler()), source_file, FakeProvider(findings=[MATCHING_FINDING]))

    (risk,) = final["risks"]
    assert risk.source is RiskSource.RULE_AND_LLM
    assert risk.risk_code == "IP_OWNER_SUPPLIER_001"
    assert risk.paragraph_index == 1, "位置来自 P9-7 的真实定位结果"


async def test_the_degraded_path_still_produces_unified_risks(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """**降级≠跳过合并**：LLM 挂掉时统一列表仍然产出，内容就是规则风险。

    若 fallback 直接收尾，一次 LLM 抽风就会让整份统一风险列表凭空消失 ——
    而这正是 §9.1 要防的"降级成仅规则结果"。
    """
    final = await _run(
        make_backend(_ok_handler()), source_file, FakeProvider(error=LLMUnavailableError("503"))
    )

    (risk,) = final["risks"]
    assert risk.source is RiskSource.RULE
    assert risk.risk_code == "IP_OWNER_SUPPLIER_001"
    assert final["llm_error_code"] == AgentErrorCode.LLM_UNAVAILABLE.value
    assert final.get("error_code") is None, "降级**不是**整次审查的失败"


async def test_the_two_source_keys_survive_the_merge(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """合并**不改写** ``rule_risks`` / ``llm_findings`` —— 三者在 State 里并存。

    它们是这次合并的输入与证据：下游要能回溯"这条风险为什么成立"，
    也要能看出"合并到底并掉了什么"。
    """
    final = await _run(make_backend(_ok_handler()), source_file, FakeProvider(findings=[MATCHING_FINDING]))

    assert isinstance(final["rule_risks"][0], RuleRisk)
    assert isinstance(final["llm_findings"][0], ResolvedFinding)
    assert len(final["risks"]) == 1, "两条来源 → 一条统一风险项"


async def test_a_run_without_any_risk_ends_with_an_empty_unified_list(
    make_backend: Callable[[Handler], BackendClient], tmp_path: Path
) -> None:
    """两侧都没命中 → ``risks=[]`` —— 是**结论**，不是"我们没跑"。

    这份文档里没有 IP 条款（规则的目标条款类型是 ``IP``，因此它拿不到可审的条款），
    模型也不报任何发现 —— 于是合并的输入为空，输出是**空列表而不是缺失**。
    """
    plain = tmp_path / "no-risk.docx"
    plain.write_bytes(docx_bytes("第一条 交付", "乙方应当在合同签订后三十日内交付全部设备。"))

    final = await _run(make_backend(_ok_handler()), plain, FakeProvider(findings=[]))

    assert final["rule_risks"] == [], "规则没有可审的目标条款"
    assert "llm_findings" in final
    assert final["risks"] == []
    assert final.get("error_code") is None
