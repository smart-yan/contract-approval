"""``merge_risks`` 节点（P9-9 接入）。

节点这一层**只做 State 编解码**，因此这里测的是三件事：

1. 两条来源各自进来、出来时是**统一形状**（映射真的被调用了，而不是被绕开）
2. 合并的**判定**不被节点重新实现一遍（它只调用领域层）
3. 失败语义：**这个节点没有失败** —— 不写 ``error_code``、不抛异常

判定逻辑本身的边界由 ``tests/unit/test_risk_merge.py`` 负责（47 个用例），
这里**不重复**测它，只测"接线接对了没有"。
"""

from __future__ import annotations

import ast
import inspect
from importlib import import_module

import pytest

from app.core.constants import RiskSource
from app.graph.builder import (
    NODE_LLM_REVIEW,
    NODE_MERGE_RISKS,
    NODE_RULE_REVIEW,
    build_review_graph,
)
from app.graph.nodes.merge_risks import merge_risks
from app.graph.state import ContractReviewState
from app.llm.finding_resolution import ResolvedFinding
from app.llm.findings import LLMFinding
from app.risk.schemas import AgentRiskItem
from app.rules.schemas import RuleRisk
from app.understanding.locator import ANCHOR_CLAUSE_FALLBACK, ANCHOR_CLAUSE_SCOPED

#: ⚠️ ``app.graph.nodes.merge_risks`` 这个**名字**在包命名空间里指的是函数而不是模块
#: （``nodes/__init__.py`` 重导出了同名函数）。要 monkeypatch 或读源码时必须拿模块对象。
_MERGE_RISKS_MODULE = import_module("app.graph.nodes.merge_risks")

_PARAGRAPH = 23
_PARAGRAPH_TEXT = "本项目产生的知识产权归乙方所有。"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _rule_risk(**overrides) -> RuleRisk:
    payload = {
        "risk_code": "IP_OWNER_SUPPLIER_001",
        "risk_title": "知识产权归属相对方",
        "dimension": "知识产权",
        "risk_level": "HIGH",
        "reason": "命中规则关键词：「知识产权归乙方」",
        "legal_basis": "《民法典》第 843 条",
        "original_text": _PARAGRAPH_TEXT,
        "paragraph_index": _PARAGRAPH,
        "quote": "知识产权归乙方",
    }
    payload.update(overrides)
    return RuleRisk(**payload)


def _resolved(**overrides) -> ResolvedFinding:
    finding = LLMFinding(
        clause_index=0,
        dimension="知识产权",
        risk_title="知识产权归属供方",
        risk_level="MEDIUM",
        reason="成果归属供方会限制我方后续使用。",
        quote="知识产权归乙方所有",
        context_before="本项目产生的",
        context_after="。",
    )
    payload = {
        "finding": finding,
        "paragraph_index": _PARAGRAPH,
        "original_text": _PARAGRAPH_TEXT,
        "quote": "知识产权归乙方所有",
        "anchor_method": ANCHOR_CLAUSE_SCOPED,
    }
    payload.update(overrides)
    return ResolvedFinding(**payload)


def _state(**overrides) -> ContractReviewState:
    return ContractReviewState(**overrides)


# --------------------------------------------------------------------------- #
# 10. 空输入
# --------------------------------------------------------------------------- #
def test_no_risks_on_either_side_yields_an_empty_list() -> None:
    """规则没命中 + 模型没报 → ``risks=[]``。这是**正常结论**，不是失败。"""
    result = merge_risks(_state(rule_risks=[], llm_findings=[]))

    assert result == {"risks": []}
    assert "error_code" not in result, "空结果是结论，不是错误"


def test_a_state_without_either_key_still_yields_an_empty_list() -> None:
    """两个键都**缺失**（不是空列表）：仍然是空结论，不抛异常。

    ``rule_risks`` 缺失有真实来源：``rule_review`` 没拿到规则快照时**刻意不写它**
    （写空列表会被读成"跑了，0 条"）。
    """
    assert merge_risks(_state()) == {"risks": []}


# --------------------------------------------------------------------------- #
# 1. Rule-only
# --------------------------------------------------------------------------- #
def test_rule_risks_alone_become_unified_risks() -> None:
    result = merge_risks(_state(rule_risks=[_rule_risk()]))

    (risk,) = result["risks"]
    assert isinstance(risk, AgentRiskItem), "出来的是**统一形状**，不是 RuleRisk"
    assert risk.source is RiskSource.RULE
    assert risk.risk_code == "IP_OWNER_SUPPLIER_001"
    assert risk.dimension == "知识产权", "规则的维度原样搬过来"
    assert risk.quote in risk.original_text


def test_several_rule_risks_are_all_carried_over() -> None:
    """不同规则命中 → 各自一条，**不因为"都是规则来源"就被并掉**。"""
    state = _state(
        rule_risks=[
            _rule_risk(),
            _rule_risk(risk_code="LIAB_UNLIMITED_001", dimension="违约责任", paragraph_index=30),
        ]
    )

    result = merge_risks(state)

    assert [risk.risk_code for risk in result["risks"]] == [
        "IP_OWNER_SUPPLIER_001",
        "LIAB_UNLIMITED_001",
    ]


# --------------------------------------------------------------------------- #
# 2. LLM-only
# --------------------------------------------------------------------------- #
def test_llm_findings_alone_become_unified_risks() -> None:
    result = merge_risks(_state(llm_findings=[_resolved()]))

    (risk,) = result["risks"]
    assert isinstance(risk, AgentRiskItem)
    assert risk.source is RiskSource.LLM, "没有规则参与，**不是** RULE+LLM"
    assert risk.risk_code is None, "模型没有稳定编码，也不许节点给它编一个"
    assert risk.risk_title == "知识产权归属供方"
    assert risk.anchor_method == ANCHOR_CLAUSE_SCOPED


# --------------------------------------------------------------------------- #
# 9. paragraph_index 的来源
# --------------------------------------------------------------------------- #
def test_the_paragraph_index_comes_from_the_resolved_position() -> None:
    """位置只能取 ``ResolvedFinding`` 上**已经核过**的那一份。

    ``LLMFinding`` 里只有 ``clause_index``（模型手里的编号），**没有段落号** ——
    若节点"顺手"去反推，这里就会得到别的值（或直接报错）。
    """
    resolved = _resolved(paragraph_index=7, quote="知识产权归乙方所有")

    result = merge_risks(_state(llm_findings=[resolved]))

    (risk,) = result["risks"]
    assert risk.paragraph_index == 7
    assert not hasattr(resolved.finding, "paragraph_index"), "模型根本不产出坐标系"


def test_the_anchor_method_is_carried_through_the_whole_way() -> None:
    """定位方式一路走到统一风险项 —— 它是"这次定位可信吗"的信号，不能被节点吃掉。"""
    fallback = _resolved(anchor_method=ANCHOR_CLAUSE_FALLBACK, quote=_PARAGRAPH_TEXT)

    result = merge_risks(_state(llm_findings=[fallback]))

    assert result["risks"][0].anchor_method == ANCHOR_CLAUSE_FALLBACK


# --------------------------------------------------------------------------- #
# 3 / 4. 两条来源相遇
# --------------------------------------------------------------------------- #
def test_a_matching_pair_is_merged_into_one_risk() -> None:
    """模型的那句"我就是规则 IP_OWNER_SUPPLIER_001" + 同段 → **一条** ``RULE+LLM``。"""
    resolved = _resolved(
        finding=LLMFinding(
            clause_index=0,
            dimension="知识产权",
            risk_title="知识产权归属供方",
            risk_level="LOW",
            reason="成果归属供方会限制我方后续使用。",
            quote="知识产权归乙方所有",
            related_rule_code="IP_OWNER_SUPPLIER_001",
        )
    )

    result = merge_risks(_state(rule_risks=[_rule_risk()], llm_findings=[resolved]))

    (risk,) = result["risks"]
    assert risk.source is RiskSource.RULE_AND_LLM
    assert risk.risk_code == "IP_OWNER_SUPPLIER_001", "编码取规则那一侧"
    assert risk.related_rule_code == "IP_OWNER_SUPPLIER_001", "关联被保留下来"
    assert risk.risk_level == "HIGH", "规则 HIGH / 模型 LOW → 取高"


def test_a_pair_that_does_not_match_stays_two_risks() -> None:
    """没有那句关联声称 → **两条**。节点把判定权完全交给领域层，自己不"看着像就并"。"""
    result = merge_risks(_state(rule_risks=[_rule_risk()], llm_findings=[_resolved()]))

    assert len(result["risks"]) == 2
    assert [risk.source for risk in result["risks"]] == [RiskSource.RULE, RiskSource.LLM]


def test_a_pair_at_different_paragraphs_stays_two_risks() -> None:
    """关联编码对上了，但**不在同一段** —— 位置由领域层把关，节点不做例外。"""
    resolved = _resolved(paragraph_index=99, original_text="另一段的原文。", quote="另一段")

    result = merge_risks(_state(rule_risks=[_rule_risk()], llm_findings=[resolved]))

    assert len(result["risks"]) == 2


# --------------------------------------------------------------------------- #
# 5. LLM 降级：合并照常执行
# --------------------------------------------------------------------------- #
def test_a_degraded_llm_still_results_in_merged_rule_risks() -> None:
    """**核心要求**：LLM 失败时 ``llm_findings`` 根本不在 State 里，合并仍然要跑。

    结果就是"仅规则风险" —— 一条不少，而且**已经是统一形状**（下游不必再分情况）。
    """
    state = _state(
        rule_risks=[_rule_risk()],
        llm_error_code="LLM_UNAVAILABLE",
        llm_error_message="LLM 返回 503",
    )

    result = merge_risks(state)

    (risk,) = result["risks"]
    assert risk.source is RiskSource.RULE
    assert risk.risk_code == "IP_OWNER_SUPPLIER_001"


def test_the_node_never_writes_a_failure_signal() -> None:
    """合并**不会**让整次审查失败：既不写 ``error_code``，也不动 ``llm_error_*``。

    LLM 的降级信号属于 ``llm_review``，整次审查的失败属于产出它的那个节点 ——
    这个节点**只读不写**那两条通道。
    """
    state = _state(rule_risks=[_rule_risk()], llm_error_code="LLM_SCHEMA_INVALID")

    result = merge_risks(state)

    assert set(result) == {"risks"}, "只写 risks 这一个键"


def test_the_node_still_runs_when_rule_review_failed() -> None:
    """规则审查失败（``error_code`` 已由 ``rule_review`` 写下）时图并没有停。

    节点照常执行（规则侧没输入 → 只剩模型风险），并且**不去改写那个 error_code** ——
    "这次审查失败"仍然由它原来的产出者说了算。
    """
    state = _state(
        error_code="AGENT_INPUT_INVALID",
        error_message="规则审查缺少必需输入",
        llm_findings=[_resolved()],
    )

    result = merge_risks(state)

    assert set(result) == {"risks"}
    assert result["risks"][0].source is RiskSource.LLM
    assert state["error_code"] == "AGENT_INPUT_INVALID", "不覆盖上游的失败判定"


def test_an_empty_document_yields_no_risks() -> None:
    """空文档：两侧都是空列表 → ``risks=[]``（与规则侧"空文档全部 NOT_MATCHED"同口径）。"""
    assert merge_risks(_state(rule_risks=[], llm_findings=[]))["risks"] == []


# --------------------------------------------------------------------------- #
# 6. 输入只读
# --------------------------------------------------------------------------- #
def test_the_inputs_are_left_untouched() -> None:
    """``rule_risks`` / ``llm_findings`` **不因合并而被改写或清空**。

    它们是这次合并的**输入与证据**：下游要能回溯"这条风险为什么成立"，
    也要能看出"合并到底并掉了什么"。
    """
    rule_risk = _rule_risk()
    resolved = _resolved()
    rule_before = _rule_risk()
    resolved_before = _resolved()

    merge_risks(_state(rule_risks=[rule_risk], llm_findings=[resolved]))

    assert rule_risk == rule_before
    assert resolved == resolved_before
    assert resolved.finding == _resolved().finding, "嵌套的 finding 也不能被改"


def test_the_state_keys_are_not_replaced_by_the_unified_list() -> None:
    """合并**不覆盖**那两个键 —— 它们与 ``risks`` 语义不同，三者并存。"""
    state = _state(rule_risks=[_rule_risk()], llm_findings=[_resolved()])

    result = merge_risks(state)

    assert set(result) == {"risks"}, "节点只写 risks，不回写那两个键"
    assert isinstance(state["rule_risks"][0], RuleRisk)
    assert isinstance(state["llm_findings"][0], ResolvedFinding)


# --------------------------------------------------------------------------- #
# 7. 节点不碰 Backend，也不重新实现映射/合并
# --------------------------------------------------------------------------- #
def test_the_node_depends_on_nothing_but_state_and_the_domain() -> None:
    """依赖集合被钉死：不认识 HTTP / Backend / 数据库。

    "合并节点会不会顺手去 Backend 补点什么"是这一步最该防的事 ——
    规则与风险的持久化是 Backend 的职责（架构 §17），Agent 侧只做编排。
    """
    tree = ast.parse(inspect.getsource(_MERGE_RISKS_MODULE))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert modules == {
        "__future__",
        "app.core.constants",
        "app.graph.state",
        "app.risk.merge",
        "app.risk.schemas",
        "app.risk.unify",
        "logging",
    }
    assert not any("httpx" in name or "backend" in name for name in modules)


def test_the_node_calls_the_domain_functions_instead_of_reimplementing_them() -> None:
    """映射与合并**必须**是调用领域层 —— 节点里不许出现第二套字段赋值。

    （``unify`` 与 ``merge`` 都已各自独立通过 Review；在这里重写一遍意味着
    两套实现会开始漂移，而且不会有任何测试报错。）
    """
    tree = ast.parse(inspect.getsource(_MERGE_RISKS_MODULE))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert {"unify_rule_risk", "unify_llm_finding", "merge_risk_items"} <= called, (
        "三个领域函数都必须被真实调用"
    )
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AgentRiskItem"
        for node in ast.walk(tree)
    ), "节点不许自己 new 一个统一风险项（那就是第二套映射）"


# --------------------------------------------------------------------------- #
# 8. Graph 接线
# --------------------------------------------------------------------------- #
def test_the_graph_ends_with_the_merge_node() -> None:
    """顺序：``rule_review → llm_review → merge_risks → END``。"""
    from langgraph.graph import END

    graph = build_review_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert NODE_MERGE_RISKS in graph.nodes
    assert (NODE_RULE_REVIEW, NODE_LLM_REVIEW) in edges, "规则审查之后是模型审查"
    assert (NODE_LLM_REVIEW, NODE_MERGE_RISKS) in edges, "模型审查之后是风险合并"
    assert (NODE_MERGE_RISKS, END) in edges, "合并之后收尾"

    incoming = {edge.source for edge in graph.edges if edge.target == NODE_MERGE_RISKS}
    assert incoming == {NODE_LLM_REVIEW}, "合并只有一个入口：模型审查之后"


def test_both_llm_branches_reach_the_merge_node() -> None:
    """``continue`` 与 ``fallback`` **都**通向合并。

    降级的意思是"这次没有模型结论"，**不是**"风险合并不用做了" ——
    若 fallback 直接收尾，一次 LLM 抽风就会让整份统一风险列表凭空消失。
    """
    from app.graph.builder import LLM_ROUTES

    assert LLM_ROUTES == {"continue": NODE_MERGE_RISKS, "fallback": NODE_MERGE_RISKS}


def test_the_merge_node_has_no_conditional_branch_of_its_own() -> None:
    """合并没有分支：它只要能跑就一定产出结果，没有"往哪边走"这回事。"""
    graph = build_review_graph().get_graph()

    outgoing = [edge for edge in graph.edges if edge.source == NODE_MERGE_RISKS]

    assert len(outgoing) == 1
    assert not outgoing[0].conditional


@pytest.mark.parametrize("key", ["rule_risks", "llm_findings"])
def test_the_node_only_reads_the_two_source_keys(key: str) -> None:
    """反向确认：节点认识的输入就是那两个键 —— 它不读 ``rules`` / ``keywords`` 之类。"""
    source = inspect.getsource(_MERGE_RISKS_MODULE)

    assert f'state.get("{key}")' in source
