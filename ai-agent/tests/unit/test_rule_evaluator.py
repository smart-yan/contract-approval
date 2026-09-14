"""规则求值器纯函数测试（``rules.evaluator.evaluate_rule``）。

全部是**纯单元测试**：不启动 Backend、不发 HTTP、不进 Graph。
条款输入走真实的 P7-1 切分（``identify_clauses``），而不是手搓 ``Clause`` ——
这样 ``paragraph_index`` 的断言才真的验证了"定位能回到文档"这件事。

真实合同的验收在 ``tests/integration/test_golden_sample_rule_evaluation.py``。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.constants import ClauseType
from app.rules.evaluator import evaluate_rule
from app.rules.schemas import (
    AgentRule,
    EvaluationFailureReason,
    RuleEvaluationResult,
    RuleEvaluationStatus,
)
from app.schemas.document import ParseResult
from app.schemas.understanding import Clause
from app.understanding.clauses import identify_clauses
from tests.factories import make_parse_result, para


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _clauses(*texts: str) -> list[Clause]:
    """把若干段落文本跑成**真实切分**的条款列表。"""
    return identify_clauses(make_parse_result(*[para(text) for text in texts]))


def _rule(**overrides) -> AgentRule:
    """一条默认的 IP 关键词规则，可按需覆盖任意字段。"""
    payload = {
        "rule_code": "IP_OWNER_SUPPLIER_001",
        "rule_name": "知识产权归属相对方",
        "dimension": "知识产权",
        "rule_type": "KEYWORD",
        "expression": {"keywords": ["知识产权归乙方"], "logic": "ANY"},
        "target_clause_types": ["IP"],
        "severity": "HIGH",
        "legal_basis": "《民法典》第 843 条",
        "sort_order": 10,
    }
    payload.update(overrides)
    return AgentRule(**payload)


#: 一段 IP 条款 + 一段违约条款，用来验证 target_clause_types 的作用域
_SCOPED_DOC = (
    "第一条 知识产权",
    "知识产权归甲方所有。",
    "第二条 违约责任",
    "乙方应赔偿全部损失。",
)


# --------------------------------------------------------------------------- #
# 1~2. KEYWORD：命中 / 不命中
# --------------------------------------------------------------------------- #
def test_keyword_matched() -> None:
    clauses = _clauses("第一条 知识产权", "本项目产生的知识产权归乙方所有。")

    result = evaluate_rule(_rule(), clauses)

    assert result.status is RuleEvaluationStatus.MATCHED
    assert len(result.risks) == 1
    assert result.failure_reason is None


def test_keyword_not_matched() -> None:
    clauses = _clauses("第一条 知识产权", "本项目产生的知识产权归甲方所有。")

    result = evaluate_rule(_rule(), clauses)

    assert result.status is RuleEvaluationStatus.NOT_MATCHED
    assert result.risks == []
    assert result.failure_reason is None
    assert result.failure_message is None


def test_keyword_expression_logic_all_is_not_silently_treated_as_any() -> None:
    """``logic`` 只实现 ANY。把它当 ANY 会凭空多报风险，因此必须失败而不是猜。"""
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(expression={"keywords": ["知识产权归乙方"], "logic": "ALL"}), clauses)

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.INVALID_EXPRESSION


@pytest.mark.parametrize(
    "expression",
    [
        {},  # 缺 keywords
        {"keywords": "知识产权归乙方"},  # keywords 不是数组
        {"keywords": []},  # 空数组
        {"keywords": [""]},  # 空字符串
        {"keywords": [123]},  # 非字符串
    ],
)
def test_keyword_invalid_expression(expression: dict) -> None:
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(expression=expression), clauses)

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.INVALID_EXPRESSION
    assert result.risks == []


def test_empty_clause_list_is_not_matched_not_failed() -> None:
    """没有条款 = 文档里确实没有这个词，属于**确定的未命中**。"""
    result = evaluate_rule(_rule(), [])

    assert result.status is RuleEvaluationStatus.NOT_MATCHED


# --------------------------------------------------------------------------- #
# 3~5. target_clause_types 的作用域
# --------------------------------------------------------------------------- #
def test_target_clause_types_limits_scope() -> None:
    """关键词只出现在 LIABILITY 条款里，而规则只作用于 IP → 未命中。"""
    clauses = _clauses(*_SCOPED_DOC)

    result = evaluate_rule(
        _rule(expression={"keywords": ["赔偿全部损失"], "logic": "ANY"}, target_clause_types=["IP"]),
        clauses,
    )

    assert result.status is RuleEvaluationStatus.NOT_MATCHED
    assert result.risks == []


def test_target_clause_types_none_matches_all_clauses() -> None:
    clauses = _clauses(*_SCOPED_DOC)

    result = evaluate_rule(
        _rule(expression={"keywords": ["赔偿全部损失"], "logic": "ANY"}, target_clause_types=None),
        clauses,
    )

    assert result.status is RuleEvaluationStatus.MATCHED
    assert result.risks[0].paragraph_index == 3


def test_empty_target_clause_types_means_unrestricted() -> None:
    """``[]`` 与 ``None`` 同义（不限）—— 空列表不能被当成"作用范围为空"。"""
    clauses = _clauses(*_SCOPED_DOC)

    result = evaluate_rule(
        _rule(expression={"keywords": ["赔偿全部损失"], "logic": "ANY"}, target_clause_types=[]),
        clauses,
    )

    assert result.status is RuleEvaluationStatus.MATCHED
    assert result.risks[0].paragraph_index == 3


def test_only_the_target_clause_of_many_is_reported() -> None:
    """两个同类型的 IP 条款，只有含关键词的那一条产生风险。"""
    clauses = _clauses(
        "第一条 知识产权",
        "知识产权归甲方所有。",
        "第二条 知识产权",
        "知识产权归乙方所有。",
    )

    result = evaluate_rule(_rule(), clauses)

    assert result.status is RuleEvaluationStatus.MATCHED
    assert [risk.paragraph_index for risk in result.risks] == [3]


def test_non_target_clause_is_never_reported_even_when_it_matches() -> None:
    """同一个词在**非目标条款**里也出现时，那一条绝不能报出来。

    作用域是**过滤**，不是"目标条款优先"—— 否则规则配了 ``target_clause_types``
    也等于全文档生效，只是排序不同。
    """
    clauses = _clauses(
        "第一条 知识产权",
        "知识产权归乙方所有。",
        "第二条 违约责任",
        "若知识产权归乙方引发争议，乙方赔偿全部损失。",
    )

    result = evaluate_rule(
        _rule(expression={"keywords": ["知识产权归乙方"], "logic": "ANY"}, target_clause_types=["IP"]),
        clauses,
    )

    assert [risk.paragraph_index for risk in result.risks] == [1]


# --------------------------------------------------------------------------- #
# target_clause_types 的**配置合法性**（与上面的作用域过滤是两件事）
#
# 未知类型不会匹配任何 Clause —— 不检查的话整条规则会静默变成 NOT_MATCHED，
# 而那是"合同确定没有风险"的意思。配置读不懂 ≠ 合同没问题。
# --------------------------------------------------------------------------- #
def test_valid_target_clause_types_still_evaluate_normally() -> None:
    """对照组：合法配置不受新增校验影响，该命中还是命中。"""
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(target_clause_types=["IP", "LIABILITY"]), clauses)

    assert result.status is RuleEvaluationStatus.MATCHED


@pytest.mark.parametrize("clause_type", [member.value for member in ClauseType])
def test_every_known_clause_type_is_accepted(clause_type: str) -> None:
    """11 个合法 ClauseType 一个都不能被误判成非法 —— 校验集合不能比枚举窄。"""
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(target_clause_types=[clause_type]), clauses)

    assert result.failure_reason is not EvaluationFailureReason.INVALID_TARGET_CLAUSE_TYPE


@pytest.mark.parametrize(
    "target_clause_types",
    [
        ["liability"],  # 小写 —— 不做归一化，必须报错
        ["LIABILITYX"],  # 打错字
        ["IP", "NOPE"],  # 合法与非法混在一起
        [""],  # 空字符串
        ["知识产权"],  # 拿中文展示名当类型
    ],
)
def test_unknown_target_clause_type_is_evaluation_failed(target_clause_types: list[str]) -> None:
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(target_clause_types=target_clause_types), clauses)

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.INVALID_TARGET_CLAUSE_TYPE


def test_invalid_target_clause_type_is_never_not_matched() -> None:
    """**最关键的一条**：配置读不懂时的结论不能是"合同确定没有这个风险"。"""
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(target_clause_types=["liability"]), clauses)

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.status is not RuleEvaluationStatus.NOT_MATCHED


def test_invalid_target_clause_type_produces_no_risk() -> None:
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(target_clause_types=["liability"]), clauses)

    assert result.risks == []


def test_invalid_target_clause_type_is_checked_before_rule_type_dispatch() -> None:
    """作用范围读不懂时，先报作用范围 —— 与 rule_type 是否已实现无关。"""
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(rule_type="MISSING", expression={}, target_clause_types=["nope"]), clauses)

    assert result.failure_reason is EvaluationFailureReason.INVALID_TARGET_CLAUSE_TYPE


@pytest.mark.parametrize("target_clause_types", [None, []])
def test_unrestricted_target_clause_types_are_not_flagged_as_invalid(
    target_clause_types: list[str] | None,
) -> None:
    """``None`` / ``[]`` 表示不限，**不是**配置错误 —— 新增校验不能把它们一起打掉。"""
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(target_clause_types=target_clause_types), clauses)

    assert result.status is RuleEvaluationStatus.MATCHED


def test_multiple_matches_are_reported_in_document_order() -> None:
    clauses = _clauses(
        "第一条 知识产权",
        "知识产权归乙方所有。",
        "第二条 知识产权",
        "知识产权归乙方所有，甲方不得使用。",
    )

    result = evaluate_rule(_rule(), clauses)

    assert [risk.paragraph_index for risk in result.risks] == [1, 3]


def test_one_risk_per_paragraph_even_with_several_keywords() -> None:
    """同一段落命中多个关键词只产出一条风险 —— 风险是"这里被触发"，不是词频。"""
    clauses = _clauses("第一条 违约责任", "乙方赔偿全部损失且承担一切责任。")

    result = evaluate_rule(
        _rule(
            expression={"keywords": ["全部损失", "承担一切责任"], "logic": "ANY"},
            target_clause_types=["LIABILITY"],
        ),
        clauses,
    )

    assert len(result.risks) == 1
    assert result.risks[0].quote == "全部损失", "quote 取原文中最靠左的命中"
    assert "承担一切责任" in result.risks[0].reason, "其余命中词出现在 reason 里，信息不丢"


# --------------------------------------------------------------------------- #
# 6. 定位：paragraph_index / quote 必须回到文档
# --------------------------------------------------------------------------- #
def test_paragraph_index_locates_the_paragraph_inside_a_multi_paragraph_clause() -> None:
    """条款跨 4 段，命中在第 3 段 —— 位置必须精确到**段**，不是条款首段。"""
    clauses = _clauses(
        "第一条 付款",
        "甲方应在签约后付款。",
        "预付款为合同总额的 30%。",
        "乙方开具发票。",
    )

    result = evaluate_rule(
        _rule(
            expression={"keywords": ["预付款为合同总额"], "logic": "ANY"},
            target_clause_types=["AMOUNT_PAYMENT"],
        ),
        clauses,
    )

    risk = result.risks[0]
    assert risk.paragraph_index == 2
    assert risk.original_text == "预付款为合同总额的 30%。"
    assert risk.quote == "预付款为合同总额"


# --------------------------------------------------------------------------- #
# 7~9. REGEX
# --------------------------------------------------------------------------- #
def test_regex_matched() -> None:
    clauses = _clauses("第一条 付款", "甲方应在验收合格后 15 日内付款。")

    result = evaluate_rule(
        _rule(
            rule_code="PAY_ACCEPTANCE_001",
            rule_type="REGEX",
            expression={"pattern": r"验收合格后\s*\d+\s*日内"},
            target_clause_types=["AMOUNT_PAYMENT"],
        ),
        clauses,
    )

    assert result.status is RuleEvaluationStatus.MATCHED
    assert result.risks[0].quote == "验收合格后 15 日内"
    assert result.risks[0].paragraph_index == 1


def test_regex_not_matched() -> None:
    clauses = _clauses("第一条 付款", "甲方应在签约后 15 日内付款。")

    result = evaluate_rule(
        _rule(rule_type="REGEX", expression={"pattern": r"验收合格后\s*\d+\s*日内"}),
        clauses,
    )

    assert result.status is RuleEvaluationStatus.NOT_MATCHED


def test_invalid_regex_is_evaluation_failed_not_an_exception() -> None:
    """非法正则不能炸掉调用方 —— 但它也**不是**未命中，是这条规则写错了。"""
    clauses = _clauses("第一条 付款", "甲方应在签约后 15 日内付款。")

    result = evaluate_rule(
        _rule(rule_type="REGEX", expression={"pattern": "([未闭合的字符组"}),
        clauses,
    )

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.INVALID_EXPRESSION
    assert result.risks == []
    assert "正则" in result.failure_message


@pytest.mark.parametrize("expression", [{}, {"pattern": ""}, {"pattern": 123}])
def test_regex_missing_pattern_is_evaluation_failed(expression: dict) -> None:
    result = evaluate_rule(_rule(rule_type="REGEX", expression=expression), _clauses("第一条 付款", "付款。"))

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.INVALID_EXPRESSION


def test_regex_zero_width_match_produces_no_risk() -> None:
    """零宽匹配没有可引用的原文 —— 不产出无法人工核对的证据。"""
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(rule_type="REGEX", expression={"pattern": r"\d*"}), clauses)

    assert result.status is RuleEvaluationStatus.NOT_MATCHED
    assert result.risks == []


# --------------------------------------------------------------------------- #
# 10~11. EXISTS / MISSING / THRESHOLD：无契约或无输入时**不得**判成未命中
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rule_type", ["EXISTS", "MISSING"])
def test_exists_and_missing_are_not_reported_as_not_matched(rule_type: str) -> None:
    """这两类规则没有 expression 契约（§7.2 无示例），P8-1 不发明 —— 显式无法求值。"""
    clauses = _clauses("第一条 知识产权", "知识产权归甲方所有。")

    result = evaluate_rule(_rule(rule_type=rule_type, expression={}), clauses)

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.UNSUPPORTED_RULE_TYPE
    assert result.status is not RuleEvaluationStatus.NOT_MATCHED
    assert result.risks == []


def test_unknown_rule_type_is_evaluation_failed() -> None:
    clauses = _clauses("第一条 知识产权", "知识产权归甲方所有。")

    result = evaluate_rule(_rule(rule_type="SEMANTIC", expression={"prompt": "看看有没有问题"}), clauses)

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.UNSUPPORTED_RULE_TYPE


def test_threshold_without_value_source_is_not_reported_as_not_matched() -> None:
    """GAP-C：``prepay_ratio`` 没有被 P7-2 抽取，因此**算不出来** —— 不是"没超标"。"""
    clauses = _clauses("第一条 付款", "预付款为合同总额的 30%。")

    result = evaluate_rule(
        _rule(
            rule_code="PAY_PREPAY_RATIO_001",
            rule_name="预付款比例超过 30%",
            dimension="金额支付",
            rule_type="THRESHOLD",
            expression={"field": "prepay_ratio", "op": "gt", "value": 0.3},
            target_clause_types=["AMOUNT_PAYMENT"],
            severity="MEDIUM",
        ),
        clauses,
    )

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.MISSING_INPUT
    assert result.status is not RuleEvaluationStatus.NOT_MATCHED
    assert result.risks == []
    assert "prepay_ratio" in result.failure_message


@pytest.mark.parametrize(
    "expression",
    [
        {"op": "gt", "value": 0.3},  # 缺 field
        {"field": "", "op": "gt", "value": 0.3},  # field 为空
        {"field": "prepay_ratio", "value": 0.3},  # 缺 op
        {"field": "prepay_ratio", "op": "gt"},  # 缺 value
        {"field": "prepay_ratio", "op": "gt", "value": "0.3"},  # value 不是数字
        {"field": "prepay_ratio", "op": "gt", "value": True},  # bool 不是数字
    ],
)
def test_threshold_invalid_expression(expression: dict) -> None:
    clauses = _clauses("第一条 付款", "预付款为合同总额的 30%。")

    result = evaluate_rule(_rule(rule_type="THRESHOLD", expression=expression), clauses)

    assert result.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert result.failure_reason is EvaluationFailureReason.INVALID_EXPRESSION


# --------------------------------------------------------------------------- #
# 13~14. 风险结果的来源与原文
# --------------------------------------------------------------------------- #
def test_risk_source_is_always_rule() -> None:
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    result = evaluate_rule(_rule(), clauses)

    assert {risk.source for risk in result.risks} == {"RULE"}


def test_risk_fields_come_from_the_rule() -> None:
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")

    risk = evaluate_rule(_rule(), clauses).risks[0]

    assert risk.risk_code == "IP_OWNER_SUPPLIER_001"
    assert risk.risk_title == "知识产权归属相对方"
    assert risk.dimension == "知识产权"
    assert risk.risk_level == "HIGH"
    assert risk.legal_basis == "《民法典》第 843 条"
    assert risk.reason, "命中理由必须非空"


def test_no_risk_is_invented_when_nothing_matches() -> None:
    clauses = _clauses("第一条 知识产权", "双方另行约定。")

    result = evaluate_rule(_rule(), clauses)

    assert result.risks == []


def test_every_evidence_string_exists_in_the_document() -> None:
    """``original_text`` / ``quote`` 必须是文档里**已有**的文本 —— 抄来的，不是写出来的。"""
    parsed: ParseResult = make_parse_result(
        para("第一条 知识产权"),
        para("知识产权归乙方所有。"),
        para("第二条 违约责任"),
        para("乙方赔偿全部损失。"),
    )
    clauses = identify_clauses(parsed)
    paragraph_texts = {paragraph.text for paragraph in parsed.paragraphs}

    result = evaluate_rule(
        _rule(
            expression={"keywords": ["知识产权归乙方", "全部损失"], "logic": "ANY"},
            target_clause_types=["IP", "LIABILITY"],
        ),
        clauses,
    )

    assert len(result.risks) == 2
    for risk in result.risks:
        assert risk.original_text in paragraph_texts
        assert risk.quote in risk.original_text
        assert risk.paragraph_index < len(parsed.paragraphs)


# --------------------------------------------------------------------------- #
# 15. 多条规则互不污染
# --------------------------------------------------------------------------- #
def test_rules_are_evaluated_independently() -> None:
    """一条规则求值失败，不影响另一条规则的结论；同一规则重复求值结果一致。"""
    clauses = _clauses("第一条 知识产权", "知识产权归乙方所有。")
    failing_rule = _rule(
        rule_code="PAY_PREPAY_RATIO_001",
        rule_type="THRESHOLD",
        expression={"field": "prepay_ratio", "op": "gt", "value": 0.3},
    )

    before = evaluate_rule(_rule(), clauses)
    failed = evaluate_rule(failing_rule, clauses)
    after = evaluate_rule(_rule(), clauses)

    assert before == after
    assert before.status is RuleEvaluationStatus.MATCHED
    assert failed.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert failed.risks == []
    assert failed.rule_code == "PAY_PREPAY_RATIO_001"
    assert {risk.risk_code for risk in before.risks} == {"IP_OWNER_SUPPLIER_001"}


# --------------------------------------------------------------------------- #
# 三态的不变量
# --------------------------------------------------------------------------- #
def test_matched_without_risks_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RuleEvaluationResult(rule_code="X", rule_name="X", status=RuleEvaluationStatus.MATCHED, risks=[])


def test_not_matched_with_failure_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RuleEvaluationResult(
            rule_code="X",
            rule_name="X",
            status=RuleEvaluationStatus.NOT_MATCHED,
            failure_reason=EvaluationFailureReason.MISSING_INPUT,
        )


def test_evaluation_failed_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        RuleEvaluationResult(rule_code="X", rule_name="X", status=RuleEvaluationStatus.EVALUATION_FAILED)
