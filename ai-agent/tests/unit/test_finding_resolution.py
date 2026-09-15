"""Finding 落地解析（``llm.finding_resolution.resolve_findings``）。

这一层的价值全在**处置 policy** 上：什么该丢、什么该留、什么该清。
因此每个用例都同时断言"结果里有没有它"和"为什么"。
"""

from __future__ import annotations

import ast
import inspect

import pytest
from pydantic import ValidationError

from app.llm.finding_resolution import ResolvedFinding, resolve_findings
from app.llm.findings import LLMFinding
from app.schemas.document import ParseResult
from app.schemas.understanding import Clause
from app.understanding.clauses import identify_clauses
from app.understanding.locator import ANCHOR_CLAUSE_FALLBACK, ANCHOR_CLAUSE_SCOPED
from tests.factories import make_parse_result, para

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
DOCUMENT = (
    "第一条 知识产权",
    "本项目产生的知识产权归乙方所有。",
    "第二条 违约责任",
    "乙方应承担违约责任。",
)


def _document(*texts: str) -> tuple[ParseResult, list[Clause]]:
    parsed = make_parse_result(*[para(text) for text in texts])
    return parsed, identify_clauses(parsed)


def _finding(**overrides) -> LLMFinding:
    payload = {
        "clause_index": 0,
        "dimension": "知识产权",
        "risk_title": "知识产权归属相对方",
        "risk_level": "HIGH",
        "reason": "成果归属供方会限制我方后续使用。",
        "quote": "知识产权归乙方所有",
        "context_before": "本项目产生的",
        "context_after": "。",
    }
    payload.update(overrides)
    return LLMFinding(**payload)


def _resolve(parsed: ParseResult, clauses: list[Clause], findings, **kwargs):
    return resolve_findings(findings=findings, clauses=clauses, paragraphs=parsed.paragraphs, **kwargs)


# --------------------------------------------------------------------------- #
# 正常落地
# --------------------------------------------------------------------------- #
def test_resolves_a_finding_to_a_real_paragraph() -> None:
    parsed, clauses = _document(*DOCUMENT)

    (resolved,) = _resolve(parsed, clauses, [_finding()])

    assert isinstance(resolved, ResolvedFinding)
    assert resolved.paragraph_index == 1
    assert resolved.anchor_method == ANCHOR_CLAUSE_SCOPED
    assert resolved.original_text == "本项目产生的知识产权归乙方所有。"
    assert resolved.quote == "知识产权归乙方所有"
    assert resolved.quote in resolved.original_text, "证据必须能在原文里找到"


def test_resolved_finding_keeps_the_model_finding_intact() -> None:
    """模型的说法原样保留（含 reason / suggestion / level）—— 定位只加位置，不改结论。"""
    parsed, clauses = _document(*DOCUMENT)
    finding = _finding(legal_basis="《民法典》第 843 条")

    (resolved,) = _resolve(parsed, clauses, [finding])

    assert resolved.finding == finding
    assert resolved.finding.reason and resolved.finding.legal_basis


def test_findings_keep_their_order() -> None:
    parsed, clauses = _document(*DOCUMENT)
    findings = [
        _finding(clause_index=1, quote="乙方应承担违约责任", risk_title="A"),
        _finding(risk_title="B"),
    ]

    resolved = _resolve(parsed, clauses, findings)

    assert [item.finding.risk_title for item in resolved] == ["A", "B"]


def test_empty_findings_yield_an_empty_result() -> None:
    parsed, clauses = _document(*DOCUMENT)

    assert _resolve(parsed, clauses, []) == []


def test_context_disambiguation_result_is_carried_out() -> None:
    """上下文在条款内唯一化了证据 —— 定位结果被如实带出。"""
    parsed, clauses = _document(
        "第一条 知识产权",
        "甲方不得主张知识产权归乙方所有。",
        "乙方也不得主张知识产权归乙方所有。",
    )

    (resolved,) = _resolve(
        parsed,
        clauses,
        [_finding(context_before="乙方也不得主张", context_after="。")],
    )

    assert resolved.paragraph_index == 2
    assert resolved.anchor_method == ANCHOR_CLAUSE_SCOPED


# --------------------------------------------------------------------------- #
# policy ①：clause_index 站不住 → 丢弃
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_index", [2, 99])
def test_out_of_range_clause_index_drops_the_finding(bad_index: int) -> None:
    """模型报了一条本文档里不存在的条款 —— 连"说的是哪条"都站不住，直接丢弃。"""
    parsed, clauses = _document(*DOCUMENT)

    resolved = _resolve(parsed, clauses, [_finding(clause_index=bad_index)])

    assert resolved == []


def test_negative_clause_index_never_even_reaches_resolution() -> None:
    """负数在**契约层**就被拒（``ge=0``）—— 它根本构不成一条 finding。

    这一层因此不必处理它：能到达解析的 clause_index 一定 ≥ 0，
    剩下的只是"是否越出本文档条款数"。
    """
    with pytest.raises(ValidationError):
        _finding(clause_index=-1)


def test_dropping_one_finding_keeps_the_others() -> None:
    parsed, clauses = _document(*DOCUMENT)
    findings = [_finding(risk_title="保留"), _finding(clause_index=99, risk_title="丢弃")]

    resolved = _resolve(parsed, clauses, findings)

    assert [item.finding.risk_title for item in resolved] == ["保留"]


# --------------------------------------------------------------------------- #
# policy ②：quote 找不到 → 保留但降级（不丢风险）
# --------------------------------------------------------------------------- #
def test_quote_not_found_keeps_the_finding_with_a_clause_level_anchor() -> None:
    """找不到证据**不等于**这条风险不存在 —— 保留，但只锚到条款级。"""
    parsed, clauses = _document(*DOCUMENT)

    (resolved,) = _resolve(parsed, clauses, [_finding(quote="这句话文档里没有")])

    assert resolved.anchor_method == ANCHOR_CLAUSE_FALLBACK
    assert resolved.paragraph_index == 0, "锚在目标条款的首个有效段落"
    assert resolved.quote == resolved.original_text, "降级时证据取回退段落的原文"
    assert resolved.finding.risk_title == "知识产权归属相对方", "风险本身必须保留"


def test_quote_in_another_clause_is_not_stolen() -> None:
    """证据只在**别的**条款里出现 → 不跨条款抢定位，降级到目标条款。"""
    parsed, clauses = _document(
        "第一条 知识产权",
        "知识产权归甲方所有。",
        "第二条 违约责任",
        "双方均应遵守知识产权归乙方所有的约定。",
    )

    (resolved,) = _resolve(parsed, clauses, [_finding(quote="知识产权归乙方所有")])

    assert resolved.anchor_method == ANCHOR_CLAUSE_FALLBACK
    assert resolved.paragraph_index <= clauses[0].end_paragraph_index


# --------------------------------------------------------------------------- #
# dimension 随 finding 一起保留（P9-8a）
# --------------------------------------------------------------------------- #
def test_dimension_survives_resolution() -> None:
    """落地解析**只加位置、不动结论** —— 维度原样跟着 finding 走。

    （P9-4 的定位规则一行未改：它不读、也不改写 ``finding`` 里的任何字段。）
    """
    parsed, clauses = _document(*DOCUMENT)

    (resolved,) = _resolve(parsed, clauses, [_finding(dimension="知识产权")])

    assert resolved.finding.dimension == "知识产权"


def test_resolution_does_not_derive_a_dimension() -> None:
    """即使命中的是 IP 条款，也不许把维度改写成与条款类型相关的东西。"""
    parsed, clauses = _document(*DOCUMENT)

    (resolved,) = _resolve(parsed, clauses, [_finding(dimension="条款完备性")])

    assert resolved.finding.dimension == "条款完备性", "维度是模型给的，不是从 clause_type 推的"


# --------------------------------------------------------------------------- #
# policy ③：related_rule_code 必须能被核对
# --------------------------------------------------------------------------- #
def test_related_rule_code_is_kept_when_it_exists_in_the_snapshot() -> None:
    parsed, clauses = _document(*DOCUMENT)

    (resolved,) = _resolve(
        parsed,
        clauses,
        [_finding(related_rule_code="IP_OWNER_SUPPLIER_001")],
        known_rule_codes={"IP_OWNER_SUPPLIER_001", "LIAB_UNLIMITED_001"},
    )

    assert resolved.finding.related_rule_code == "IP_OWNER_SUPPLIER_001"


def test_unknown_related_rule_code_is_cleared_but_the_risk_survives() -> None:
    """模型编了一个规则编码 —— 关联作废，**风险保留**（丢关联只是少条线索）。"""
    parsed, clauses = _document(*DOCUMENT)

    (resolved,) = _resolve(
        parsed,
        clauses,
        [_finding(related_rule_code="NOT_A_REAL_RULE")],
        known_rule_codes={"IP_OWNER_SUPPLIER_001"},
    )

    assert resolved.finding.related_rule_code is None
    assert resolved.finding.risk_title == "知识产权归属相对方"
    assert resolved.paragraph_index == 1


def test_related_rule_code_none_stays_none() -> None:
    parsed, clauses = _document(*DOCUMENT)

    (resolved,) = _resolve(parsed, clauses, [_finding()], known_rule_codes={"ANY"})

    assert resolved.finding.related_rule_code is None


def test_missing_snapshot_clears_every_relation() -> None:
    """调用方没给规则快照（缺省空集合）→ 一个关联都不接受。

    方向是保守的：丢掉关联只会少一条线索，留下假关联会让人以为两件事有关。
    """
    parsed, clauses = _document(*DOCUMENT)

    (resolved,) = _resolve(parsed, clauses, [_finding(related_rule_code="IP_OWNER_SUPPLIER_001")])

    assert resolved.finding.related_rule_code is None


def test_original_finding_is_not_mutated() -> None:
    """清空关联返回的是**副本** —— 输入对象保持原样（可复现、可重跑）。"""
    parsed, clauses = _document(*DOCUMENT)
    finding = _finding(related_rule_code="NOT_A_REAL_RULE")

    _resolve(parsed, clauses, [finding], known_rule_codes={"IP_OWNER_SUPPLIER_001"})

    assert finding.related_rule_code == "NOT_A_REAL_RULE"


def test_relation_is_verified_even_for_a_fallback_finding() -> None:
    """定位降级不影响关联核对 —— 两件事各自独立判定。"""
    parsed, clauses = _document(*DOCUMENT)

    (resolved,) = _resolve(
        parsed,
        clauses,
        [_finding(quote="文档里没有这句话", related_rule_code="NOT_A_REAL_RULE")],
        known_rule_codes={"IP_OWNER_SUPPLIER_001"},
    )

    assert resolved.anchor_method == ANCHOR_CLAUSE_FALLBACK
    assert resolved.finding.related_rule_code is None


# --------------------------------------------------------------------------- #
# 不变量
# --------------------------------------------------------------------------- #
def test_never_resolves_outside_the_target_clause() -> None:
    parsed, clauses = _document(*DOCUMENT)

    for index in range(len(clauses)):
        for item in _resolve(parsed, clauses, [_finding(clause_index=index)]):
            assert clauses[index].start_paragraph_index <= item.paragraph_index
            assert item.paragraph_index <= clauses[index].end_paragraph_index


def test_every_result_points_at_a_real_paragraph() -> None:
    parsed, clauses = _document(*DOCUMENT)

    for item in _resolve(parsed, clauses, [_finding(), _finding(quote="找不到的")]):
        assert parsed.paragraphs[item.paragraph_index].text == item.original_text
        assert item.quote in item.original_text


def test_inputs_are_not_modified() -> None:
    parsed, clauses = _document(*DOCUMENT)
    paragraphs_before = [p.model_copy() for p in parsed.paragraphs]
    findings = [_finding(quote="找不到的"), _finding()]

    _resolve(parsed, clauses, findings)

    assert parsed.paragraphs == paragraphs_before
    assert [f.quote for f in findings] == ["找不到的", "知识产权归乙方所有"]


def test_resolution_is_deterministic() -> None:
    parsed, clauses = _document(*DOCUMENT)

    assert _resolve(parsed, clauses, [_finding()]) == _resolve(parsed, clauses, [_finding()])


# --------------------------------------------------------------------------- #
# 契约：本步刻意不做的东西
# --------------------------------------------------------------------------- #
def test_resolved_finding_fields_are_frozen() -> None:
    assert set(ResolvedFinding.__dataclass_fields__) == {
        "finding",
        "paragraph_index",
        "original_text",
        "quote",
        "anchor_method",
    }


@pytest.mark.parametrize("forbidden", ["source", "risk_code", "risk_level", "char_start", "anchor_score"])
def test_resolved_finding_carries_no_risk_model_fields(forbidden: str) -> None:
    """不产出风险模型：``source`` / ``risk_code`` 属于统一风险项（P9-0 已裁决暂不放宽）。"""
    assert forbidden not in ResolvedFinding.__dataclass_fields__


def test_occurrence_hint_still_does_not_select_the_location() -> None:
    """``occurrence_hint`` 仍不参与定位：只改这个字段，**定出来的位置一模一样**。

    （比的是位置本身，不是整个 ``ResolvedFinding`` —— 后者包含 finding，
    而 finding 确实因为 hint 不同而不同，那是应该的。）
    """
    parsed, clauses = _document(*DOCUMENT)

    def location(items):
        return [(i.paragraph_index, i.anchor_method, i.quote, i.original_text) for i in items]

    without = _resolve(parsed, clauses, [_finding(occurrence_hint=None)])
    with_hint = _resolve(parsed, clauses, [_finding(occurrence_hint=3)])

    assert location(without) == location(with_hint)


def test_resolution_depends_only_on_pure_code() -> None:
    """依赖集合被钉死：不调 LLM、不碰 Graph/State/Backend，也没有 char offset 工具。"""
    import app.llm.finding_resolution as module

    tree = ast.parse(inspect.getsource(module))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert modules == {
        "__future__",
        "logging",
        "collections.abc",
        "dataclasses",
        "app.llm.findings",
        "app.schemas.document",
        "app.schemas.understanding",
        "app.understanding.locator",
    }


def test_resolution_rejects_invalid_findings_at_the_contract_level() -> None:
    """入口只接受**已通过校验**的 finding（json_guard 的产物），不是原始文本。"""
    with pytest.raises(ValidationError):
        _finding(risk_level="CRITICAL")
