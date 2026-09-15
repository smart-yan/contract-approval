"""统一风险项与两个来源的映射（P9-8）。

这一层的价值是**把"来源"变成字段**：下游只认一种形状。
因此每个用例都在回答同一个问题 —— "这个字段从哪来、搬运过程中有没有走样"。
"""

from __future__ import annotations

import ast
import inspect

import pytest
from pydantic import ValidationError

from app.core.constants import RiskSource
from app.llm.finding_resolution import ResolvedFinding
from app.llm.findings import LLMFinding, Suggestion
from app.risk.schemas import AgentRiskItem
from app.risk.unify import unify_llm_finding, unify_rule_risk
from app.rules.schemas import RuleRisk
from app.understanding.locator import ANCHOR_CLAUSE_FALLBACK, ANCHOR_CLAUSE_SCOPED


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
        "original_text": "本项目产生的知识产权归乙方所有。",
        "paragraph_index": 23,
        "quote": "知识产权归乙方",
    }
    payload.update(overrides)
    return RuleRisk(**payload)


def _finding(**overrides) -> LLMFinding:
    payload = {
        "clause_index": 4,
        "dimension": "知识产权",
        "risk_title": "知识产权归属供方",
        "risk_level": "HIGH",
        "reason": "成果归属供方会限制我方后续使用。",
        "legal_basis": None,
        "quote": "知识产权归乙方所有",
        "context_before": "本项目产生的",
        "context_after": "。",
    }
    payload.update(overrides)
    return LLMFinding(**payload)


def _resolved(**overrides) -> ResolvedFinding:
    payload = {
        "finding": _finding(),
        "paragraph_index": 23,
        "original_text": "本项目产生的知识产权归乙方所有。",
        "quote": "知识产权归乙方所有",
        "anchor_method": ANCHOR_CLAUSE_SCOPED,
    }
    payload.update(overrides)
    return ResolvedFinding(**payload)


# --------------------------------------------------------------------------- #
# 统一模型的契约
# --------------------------------------------------------------------------- #
def test_unified_fields_are_frozen() -> None:
    assert set(AgentRiskItem.model_fields) == {
        "source",
        "risk_code",
        "risk_title",
        "dimension",
        "risk_level",
        "reason",
        "legal_basis",
        "original_text",
        "quote",
        "paragraph_index",
        "anchor_method",
        "related_rule_code",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "suggestion",
        "context_before",
        "context_after",
        "occurrence_hint",
        "clause_index",
        "clause_id",
        "task_id",
        "contract_id",
        "rule_id",
        "confidence",
        "anchor_score",
        "char_start",
        "char_end",
        "review_status",
    ],
)
def test_unified_model_carries_no_out_of_scope_fields(forbidden: str) -> None:
    """不该进来的东西一个都不许进来（各有各的归属，见 schemas 的说明）。"""
    assert forbidden not in AgentRiskItem.model_fields


def test_source_values_match_the_backend_vocabulary() -> None:
    """``RiskSource`` 的取值与 Backend 一一对应（含 ``RULE+LLM`` 的字面写法）。"""
    assert {member.value for member in RiskSource} == {"RULE", "LLM", "RULE+LLM"}


def test_quote_must_not_be_empty() -> None:
    """证据为空的风险项没有意义 —— 每一项都必须能指回原文。"""
    with pytest.raises(ValidationError):
        AgentRiskItem(
            source=RiskSource.RULE,
            risk_title="x",
            risk_level="HIGH",
            reason="y",
            original_text="z",
            quote="",
            paragraph_index=0,
        )


# --------------------------------------------------------------------------- #
# RuleRisk → 统一
# --------------------------------------------------------------------------- #
def test_rule_risk_maps_field_by_field() -> None:
    item = unify_rule_risk(_rule_risk())

    assert item.source is RiskSource.RULE
    assert item.risk_code == "IP_OWNER_SUPPLIER_001"
    assert item.risk_title == "知识产权归属相对方"
    assert item.dimension == "知识产权", "规则来源**有**维度"
    assert item.risk_level == "HIGH"
    assert item.reason == "命中规则关键词：「知识产权归乙方」"
    assert item.legal_basis == "《民法典》第 843 条"
    assert item.original_text == "本项目产生的知识产权归乙方所有。"
    assert item.quote == "知识产权归乙方"
    assert item.paragraph_index == 23


def test_rule_risk_has_no_anchor_method_and_no_related_rule_code() -> None:
    """规则风险有确定的段落号，**不存在**"反查方式"；它本身就是规则风险。

    给 ``anchor_method`` 编一个值（比如"规则也算 CLAUSE_SCOPED"）会让
    "这次定位可信吗"这个信号失真 —— 它是给模型定位用的。
    """
    item = unify_rule_risk(_rule_risk())

    assert item.anchor_method is None
    assert item.related_rule_code is None


def test_rule_risk_keeps_optional_fields_empty() -> None:
    item = unify_rule_risk(_rule_risk(legal_basis=None))

    assert item.legal_basis is None


# --------------------------------------------------------------------------- #
# ResolvedFinding → 统一
# --------------------------------------------------------------------------- #
def test_llm_finding_maps_field_by_field() -> None:
    item = unify_llm_finding(_resolved())

    assert item.source is RiskSource.LLM
    assert item.risk_code is None, "模型没有稳定编码，也不许它编一个"
    assert item.risk_title == "知识产权归属供方"
    assert item.risk_level == "HIGH"
    assert item.reason == "成果归属供方会限制我方后续使用。"
    assert item.original_text == "本项目产生的知识产权归乙方所有。"
    assert item.quote == "知识产权归乙方所有"
    assert item.paragraph_index == 23


def test_llm_location_comes_from_the_resolved_position_not_the_finding() -> None:
    """位置只能取**已核对**的那一份：模型手里根本没有坐标系。

    ``LLMFinding`` 里只有 ``clause_index`` 与 ``quote``；段落号是 Agent 定位出来的。
    """
    resolved = _resolved(paragraph_index=7, original_text="定位到的段落原文。", quote="定位到的")

    item = unify_llm_finding(resolved)

    assert item.paragraph_index == 7
    assert item.original_text == "定位到的段落原文。"
    assert item.quote == "定位到的"
    assert item.quote in item.original_text


def test_llm_anchor_method_is_preserved_verbatim() -> None:
    """``anchor_method`` **不能**被搬运破坏 —— 它是"这次定位可信吗"的信号。"""
    scoped = unify_llm_finding(_resolved(anchor_method=ANCHOR_CLAUSE_SCOPED))
    fallback = unify_llm_finding(
        _resolved(anchor_method=ANCHOR_CLAUSE_FALLBACK, quote="原文", original_text="原文")
    )

    assert scoped.anchor_method == ANCHOR_CLAUSE_SCOPED
    assert fallback.anchor_method == ANCHOR_CLAUSE_FALLBACK


def test_llm_related_rule_code_is_carried_over() -> None:
    """它**已经过 rule snapshot 核对**（核不上的在 P9-4 就清空了），可以直接搬运。"""
    item = unify_llm_finding(_resolved(finding=_finding(related_rule_code="IP_OWNER_SUPPLIER_001")))

    assert item.related_rule_code == "IP_OWNER_SUPPLIER_001"


def test_llm_related_rule_code_may_be_absent() -> None:
    assert unify_llm_finding(_resolved()).related_rule_code is None


def test_llm_dimension_is_carried_over_from_the_finding() -> None:
    """**P9-8a 补上了这个缺口**：模型来源的维度来自 ``LLMFinding.dimension``，原样搬运。

    （P9-8 时它还是 ``None`` —— 那一版如实暴露了"模型输出契约里没有维度"；
    P9-8a 在**上游**补上了这个必填字段，于是这里改为断言"搬过来了"。）
    """
    item = unify_llm_finding(_resolved(finding=_finding(dimension="知识产权")))

    assert item.dimension == "知识产权"
    assert "dimension" in LLMFinding.model_fields, "缺口补在**上游契约**里"


# --------------------------------------------------------------------------- #
# dimension 收紧为必填（P9-8a）
# --------------------------------------------------------------------------- #
def test_unified_dimension_is_no_longer_optional() -> None:
    """两侧现在都能保证有值，因此**不再留无意义的 Optional**（P9-8a 收紧）。"""
    assert AgentRiskItem.model_fields["dimension"].is_required()
    assert "None" not in str(AgentRiskItem.model_fields["dimension"].annotation)


@pytest.mark.parametrize("bad", [None, ""])
def test_unified_dimension_cannot_be_none_or_empty(bad: object) -> None:
    """（用重新构造而不是 ``model_copy`` —— 后者**跳过校验**，测不出任何东西。）"""
    payload = unify_rule_risk(_rule_risk()).model_dump()

    with pytest.raises(ValidationError):
        AgentRiskItem(**{**payload, "dimension": bad})


def test_unified_dimension_is_never_derived_from_the_clause_type() -> None:
    """**维度 ≠ 条款类型**：命中的是 IP 条款，维度仍然照模型说的走。

    如果映射层"顺手"用 ``clause_type`` 反推维度，这里就会得到 "IP" 而不是"知识产权" ——
    那等于把文档里的分类标签冒充成了审查维度。
    """
    resolved = _resolved(finding=_finding(clause_index=4, dimension="条款完备性"))

    item = unify_llm_finding(resolved)

    assert item.dimension == "条款完备性"


def test_no_mapper_falls_back_to_a_default_dimension() -> None:
    """映射层**不兜底**：两个来源的维度都只能来自各自的输入。

    （"编一个默认值"会把"模型选错了维度"这种问题从数据里抹掉。）
    """
    rule_item = unify_rule_risk(_rule_risk(dimension="不可抗力"))
    llm_item = unify_llm_finding(_resolved(finding=_finding(dimension="数据安全")))

    assert rule_item.dimension == "不可抗力"
    assert llm_item.dimension == "数据安全"


# --------------------------------------------------------------------------- #
# 映射的通用不变量
# --------------------------------------------------------------------------- #
def test_mapping_does_not_modify_the_input() -> None:
    """映射**只读不写** —— 输入对象在映射前后一模一样。

    （``ResolvedFinding`` 是 ``slots=True`` 的冻结 dataclass，没有 ``__dict__``，
    因此与一份等值的副本比 —— 真被就地改过就会不等。）
    """
    rule_risk = _rule_risk()
    resolved = _resolved()
    rule_before = _rule_risk()
    resolved_before = _resolved()

    unify_rule_risk(rule_risk)
    unify_llm_finding(resolved)

    assert rule_risk == rule_before
    assert resolved == resolved_before
    assert resolved.finding == _finding(), "嵌套的 finding 也不能被改"


def test_mapping_is_deterministic() -> None:
    assert unify_rule_risk(_rule_risk()) == unify_rule_risk(_rule_risk())
    assert unify_llm_finding(_resolved()) == unify_llm_finding(_resolved())


def test_both_sources_produce_the_same_shape() -> None:
    """统一的意义就在这里：下游不需要知道它从哪来。"""
    from_rule = unify_rule_risk(_rule_risk())
    from_llm = unify_llm_finding(_resolved())

    assert type(from_rule) is type(from_llm) is AgentRiskItem
    assert from_rule.source is not from_llm.source, "来源退化成**字段**，不再分叉成两条代码路径"


def test_quote_is_always_a_substring_of_original_text() -> None:
    """两种来源都必须守住这条不变量（P9-0 专门裁决过两者的同义性）。"""
    for item in (unify_rule_risk(_rule_risk()), unify_llm_finding(_resolved())):
        assert item.quote in item.original_text


def test_llm_suggestion_stays_out_of_the_risk_item() -> None:
    """内联建议**不进**风险项：建议是独立资源（Backend 有专门的表）。

    它仍留在 ``state["llm_findings"]`` 里，供建议生成阶段取用 —— 没有丢。
    """
    resolved = _resolved(finding=_finding(suggestion=Suggestion(type="REPLACE", text="改成……")))

    item = unify_llm_finding(resolved)

    assert not hasattr(item, "suggestion")
    assert resolved.finding.suggestion is not None, "输入里还在"


# --------------------------------------------------------------------------- #
# 本步刻意不做的事
# --------------------------------------------------------------------------- #
def test_no_mapper_produces_a_merged_source() -> None:
    """``RULE+LLM`` 是**合并**的产物 —— 本轮没有任何代码产出它。"""
    sources = {unify_rule_risk(_rule_risk()).source, unify_llm_finding(_resolved()).source}

    assert RiskSource.RULE_AND_LLM not in sources


def test_unify_depends_on_nothing_but_the_two_contracts() -> None:
    """依赖集合被钉死：不做合并、不碰 Graph/State/DB、不认识 HTTP。"""
    import app.risk.unify as module

    tree = ast.parse(inspect.getsource(module))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert modules == {
        "__future__",
        "app.core.constants",
        "app.llm.finding_resolution",
        "app.risk.schemas",
        "app.rules.schemas",
    }
