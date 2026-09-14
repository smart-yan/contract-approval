"""黄金样例合同上的规则求值验收（P8-1）。

``samples/采购合同-风险版.docx`` 是 P7/P8/P9/P11 的**长期验收基准**。
这里用 Backend seed 的那 3 条规则去跑它，把"哪条规则命中、命中在哪一段、
引用了哪句原文"逐条钉死 —— 这是 P8 的验收标准（架构文档 §17：示例合同命中预置规则）。

⚠️ 规则 payload **手抄自** ``backend/scripts/seed_rules.py``。Agent 不能 import Backend 的
代码（两侧是 HTTP 边界），所以这是一份**刻意的副本**：seed 改了而这里没跟，
测试仍会绿 —— 这是已知缺口。但它挡得住"Agent 侧求值行为被改坏"，
而那正是这个文件存在的理由。

⚠️ 本文件**不改动黄金样例**，只是读它。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.constants import ClauseType, RuleType
from app.parsers import parse_document_file
from app.rules import AgentRule, EvaluationFailureReason, RuleEvaluationStatus, evaluate_rule
from app.schemas.document import ParseResult
from app.schemas.understanding import Clause, MetadataItem
from app.understanding.clauses import identify_clauses
from app.understanding.metadata import extract_metadata

SAMPLE = Path(__file__).resolve().parents[3] / "samples" / "采购合同-风险版.docx"

#: 与 ``backend/scripts/seed_rules.py`` 的 RULE_SPECS 对齐（见模块 docstring 的说明）
SEED_RULES = [
    AgentRule(
        rule_code="IP_OWNER_SUPPLIER_001",
        rule_name="知识产权归属相对方",
        dimension="知识产权",
        rule_type=RuleType.KEYWORD,
        expression={
            "keywords": ["知识产权归供方", "知识产权归乙方", "知识产权归供应商", "所有权归乙方"],
            "logic": "ANY",
        },
        target_clause_types=[ClauseType.IP],
        severity="HIGH",
        sort_order=10,
    ),
    AgentRule(
        rule_code="LIAB_UNLIMITED_001",
        rule_name="我方单方承担无限责任",
        dimension="违约责任",
        rule_type=RuleType.KEYWORD,
        expression={
            "keywords": ["全部损失", "无限责任", "不设上限", "承担一切责任", "赔偿全部"],
            "logic": "ANY",
        },
        target_clause_types=[ClauseType.LIABILITY],
        severity="HIGH",
        sort_order=30,
    ),
    AgentRule(
        rule_code="PAY_PREPAY_RATIO_001",
        rule_name="预付款比例超过 30%",
        dimension="金额支付",
        rule_type=RuleType.THRESHOLD,
        expression={"field": "prepay_ratio", "op": "gt", "value": 0.3},
        target_clause_types=[ClauseType.AMOUNT_PAYMENT],
        severity="MEDIUM",
        sort_order=50,
    ),
]

#: 期望的风险命中位置：(rule_code, 段落序号, quote)
EXPECTED_HITS = [
    ("IP_OWNER_SUPPLIER_001", 23, "知识产权归乙方"),
    ("LIAB_UNLIMITED_001", 30, "全部损失"),
]


@pytest.fixture(scope="module")
def parsed() -> ParseResult:
    assert SAMPLE.is_file(), f"黄金样例缺失：{SAMPLE}（它是被提交进仓库的验收基准）"
    return parse_document_file(SAMPLE, file_type="DOCX")


@pytest.fixture(scope="module")
def clauses(parsed: ParseResult) -> list[Clause]:
    return identify_clauses(parsed)


@pytest.fixture(scope="module")
def metadata(parsed: ParseResult) -> list[MetadataItem]:
    """P7-2 抽出的元数据 —— THRESHOLD 规则的值来源（P8-3）。"""
    return extract_metadata(parsed)


def _rule(rule_code: str) -> AgentRule:
    return next(rule for rule in SEED_RULES if rule.rule_code == rule_code)


@pytest.mark.parametrize(("rule_code", "paragraph_index", "quote"), EXPECTED_HITS)
def test_seed_rule_hits_the_golden_sample(
    clauses: list[Clause], rule_code: str, paragraph_index: int, quote: str
) -> None:
    result = evaluate_rule(_rule(rule_code), clauses)

    assert result.status is RuleEvaluationStatus.MATCHED
    assert [(risk.paragraph_index, risk.quote) for risk in result.risks] == [(paragraph_index, quote)]


def test_hit_quote_is_verbatim_text_of_that_paragraph(parsed: ParseResult, clauses: list[Clause]) -> None:
    """命中必须能回指**真实文档**：段落原文 + 其中的逐字片段。"""
    for rule_code, _, _ in EXPECTED_HITS:
        for risk in evaluate_rule(_rule(rule_code), clauses).risks:
            assert risk.original_text == parsed.paragraphs[risk.paragraph_index].text
            assert risk.quote in risk.original_text
            assert risk.source == "RULE"


def test_hits_land_inside_clauses_of_the_expected_type(clauses: list[Clause]) -> None:
    """命中的段落必须落在规则限定的条款类型里 —— 作用域真的生效了。"""
    expected_types = {"IP_OWNER_SUPPLIER_001": ClauseType.IP, "LIAB_UNLIMITED_001": ClauseType.LIABILITY}

    for rule_code, paragraph_index, _ in EXPECTED_HITS:
        clause = next(
            c for c in clauses if c.start_paragraph_index <= paragraph_index <= c.end_paragraph_index
        )
        assert clause.clause_type == expected_types[rule_code]


def test_prepay_threshold_rule_gets_a_definite_answer(
    clauses: list[Clause], metadata: list[MetadataItem]
) -> None:
    """**GAP-C 已闭合**（P8-3）：付款表里的 30% 让这条规则得到确定结论。

    黄金样例的付款表写着「1. 预付款 | 30%」，规则是 ``prepay_ratio > 0.3`` ——
    30% 恰好**等于**阈值，``gt`` 不成立，所以是 ``NOT_MATCHED``（不是"没超标"的猜测，
    是真的比过了）。风险结果与改前一致：都是空。
    """
    result = evaluate_rule(_rule("PAY_PREPAY_RATIO_001"), clauses, metadata=metadata)

    assert result.status is RuleEvaluationStatus.NOT_MATCHED
    assert result.status is not RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is None
    assert result.risks == []


def test_prepay_threshold_rule_without_metadata_still_cannot_be_evaluated(
    clauses: list[Clause],
) -> None:
    """**没有值来源时结论一个字没变** —— 仍然是不能求值，绝不是"没超标"。

    这是那条防线：拿不到值就停在 ``MISSING_INPUT``，谁都不许把它折成 NOT_MATCHED。
    """
    result = evaluate_rule(_rule("PAY_PREPAY_RATIO_001"), clauses)

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.MISSING_INPUT
    assert result.risks == []


def test_prepay_ratio_actually_reaches_the_evaluator(metadata: list[MetadataItem]) -> None:
    """值是真的从文档里读出来的：0.3 来自付款表第 18 段那一行。"""
    item = next(m for m in metadata if m.field_key == "prepay_ratio")

    assert item.field_value == "0.3"
    assert item.paragraph_index == 18
