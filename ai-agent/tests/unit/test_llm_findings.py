"""``findings`` —— LLM 审查的领域契约（P9-2）。

这一层是**业务形状**：模型该吐什么、提示该喂什么。
它不调用任何东西，因此测试也只需要断言契约本身。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.llm import json_guard
from app.llm.findings import (
    ClauseContext,
    ClauseReviewPromptInput,
    LLMFinding,
    LLMReviewResult,
    MatchedRuleHint,
    Suggestion,
)


def _finding(**overrides) -> LLMFinding:
    payload = {
        "clause_index": 3,
        "dimension": "金额支付",
        "risk_title": "付款条款缺少验收前置条件",
        "risk_level": "HIGH",
        "reason": "付款义务先于验收，我方可能在未确认交付质量前即需付款。",
        "quote": "合同生效后五（5）个工作日内支付",
        "context_before": "1. 预付款",
        "context_after": "乙方完成交付后",
    }
    payload.update(overrides)
    return LLMFinding(**payload)


# --------------------------------------------------------------------------- #
# 字段完备性：P9-0 批准的 11 个字段一个都不能少
# --------------------------------------------------------------------------- #
def test_finding_has_exactly_the_agreed_fields() -> None:
    assert set(LLMFinding.model_fields) == {
        "clause_index",
        "dimension",
        "risk_title",
        "risk_level",
        "reason",
        "legal_basis",
        "quote",
        "context_before",
        "context_after",
        "occurrence_hint",
        "suggestion",
        "related_rule_code",
    }


@pytest.mark.parametrize("forbidden", ["paragraph_index", "source", "risk_code", "confidence", "char_start"])
def test_finding_does_not_carry_agent_side_fields(forbidden: str) -> None:
    """**LLM 不给段落号、不给来源、不给风险编码** —— 那些是 Agent 的记录职责。"""
    assert forbidden not in LLMFinding.model_fields


def test_normal_instantiation_with_all_fields() -> None:
    finding = _finding(
        legal_basis="《民法典》第 626 条",
        occurrence_hint=1,
        suggestion=Suggestion(
            type="REPLACE", text="建议修改为：验收合格后 15 日内支付。", reason="先验收后付款"
        ),
        related_rule_code="PAY_PREPAY_RATIO_001",
    )

    assert finding.clause_index == 3
    assert finding.risk_level == "HIGH"
    assert finding.suggestion.type == "REPLACE"
    assert finding.related_rule_code == "PAY_PREPAY_RATIO_001"


def test_minimal_instantiation_uses_defaults() -> None:
    finding = _finding()

    assert finding.legal_basis is None
    assert finding.occurrence_hint is None
    assert finding.suggestion is None
    assert finding.related_rule_code is None


# --------------------------------------------------------------------------- #
# 必填与约束
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "missing", ["clause_index", "dimension", "risk_title", "risk_level", "reason", "quote"]
)
def test_required_fields_are_enforced(missing: str) -> None:
    payload = {
        "clause_index": 3,
        "dimension": "金额支付",
        "risk_title": "x",
        "risk_level": "HIGH",
        "reason": "y",
        "quote": "z",
    }
    del payload[missing]

    with pytest.raises(ValidationError):
        LLMFinding(**payload)


# --------------------------------------------------------------------------- #
# dimension：固定词表（P9-8a）
#
# 词表取自架构文档 §11.1 的「维度」列 —— **不是这里新造的**。
# --------------------------------------------------------------------------- #
#: 与 ``RiskDimensionLiteral`` 一一对应的 10 个维度名（文档 §11.1）
EXPECTED_DIMENSIONS = {
    "主体资质",
    "金额支付",
    "违约责任",
    "知识产权",
    "争议管辖",
    "保密",
    "不可抗力",
    "数据安全",
    "验收",
    "条款完备性",
}


def test_dimension_vocabulary_comes_from_the_document() -> None:
    """维度词表**就是** §11.1 那 10 项 —— 不多不少（改它必须是有意的）。"""
    from app.llm.findings import RiskDimensionLiteral

    assert set(RiskDimensionLiteral.__args__) == EXPECTED_DIMENSIONS


@pytest.mark.parametrize("dimension", sorted(EXPECTED_DIMENSIONS))
def test_every_documented_dimension_passes(dimension: str) -> None:
    assert _finding(dimension=dimension).dimension == dimension


@pytest.mark.parametrize(
    "dimension",
    [
        "IP",  # ⚠️ 条款类型，不是维度
        "AMOUNT_PAYMENT",  # ⚠️ 同上
        "知识产权归属",  # 自造词（词表里是「知识产权」）
        "合同价款",  # 自造词
        "OTHER",  # 自造词
        "",  # 空
        None,
    ],
)
def test_invalid_dimension_is_rejected(dimension: object) -> None:
    """越界/自造的维度被 schema 直接拒绝 —— **不改写成兜底值、不做修复**（P9-1 的语义）。"""
    with pytest.raises(ValidationError):
        _finding(dimension=dimension)


def test_dimension_is_required() -> None:
    """必填：模型不能"忘了"它 —— 缺了就是这一批不合契约（走降级）。"""
    with pytest.raises(ValidationError):
        LLMFinding(
            clause_index=0,
            risk_title="x",
            risk_level="LOW",
            reason="y",
            quote="z",
        )


def test_json_guard_rejects_an_invented_dimension() -> None:
    """走 **json_guard 的真实校验路径**：自造维度 → ``LLM_SCHEMA_INVALID``。

    这是 P9-1 的严格语义在 P9-8a 上的落地：**不改成兜底值、不修复、不重试** ——
    这一批直接降级（由 ``llm_review`` 节点翻译成 ``llm_error_code``）。
    """
    import json

    from app.llm.json_guard import LLMSchemaInvalidError, parse_and_validate
    from app.llm.schemas import LLMResult

    raw = json.dumps(
        {"findings": [dict(_finding(dimension="知识产权").model_dump(), dimension="知识产权归属")]},
        ensure_ascii=False,
    )

    with pytest.raises(LLMSchemaInvalidError):
        parse_and_validate(LLMReviewResult, LLMResult(raw_text=raw, model="m", provider="p"))


def test_dimension_enum_is_visible_in_the_injected_schema() -> None:
    """固定词表必须出现在注入提示的 JSON Schema 里 —— 模型才"看得到"可选值。

    提示正文只说"从 schema 的枚举里选"，**不抄一遍列表**（抄本会漂移）。
    """
    instruction = json_guard.build_schema_instruction(LLMFinding)

    for dimension in EXPECTED_DIMENSIONS:
        assert f'"{dimension}"' in instruction


# --------------------------------------------------------------------------- #
# context_before / context_after：**Schema 硬约束**（最多 30 字，允许为空）
#
# 允许为空是必须的：quote 可能正好落在段落的开头或结尾，那里没有"紧邻的前文"。
# --------------------------------------------------------------------------- #
def test_context_fields_default_to_empty() -> None:
    finding = LLMFinding(
        clause_index=0, dimension="验收", risk_title="x", risk_level="LOW", reason="y", quote="z"
    )

    assert finding.context_before == ""
    assert finding.context_after == ""


@pytest.mark.parametrize("field", ["context_before", "context_after"])
def test_context_may_be_empty(field: str) -> None:
    assert getattr(_finding(**{field: ""}), field) == ""


@pytest.mark.parametrize("field", ["context_before", "context_after"])
def test_context_at_the_limit_is_accepted(field: str) -> None:
    assert len(getattr(_finding(**{field: "字" * 30}), field)) == 30


@pytest.mark.parametrize("field", ["context_before", "context_after"])
def test_context_over_the_limit_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _finding(**{field: "字" * 31})


@pytest.mark.parametrize("field", ["context_before", "context_after"])
def test_normal_context_text_is_accepted(field: str) -> None:
    assert getattr(_finding(**{field: "乙方完成交付后"}), field) == "乙方完成交付后"


@pytest.mark.parametrize("level", ["HIGH", "MEDIUM", "LOW"])
def test_valid_risk_levels(level: str) -> None:
    assert _finding(risk_level=level).risk_level == level


@pytest.mark.parametrize("level", ["CRITICAL", "high", "高", "", None, 1])
def test_invalid_risk_levels_are_rejected(level: object) -> None:
    """越界的等级会被 Pydantic 直接拒绝 —— 而它会先出现在注入提示的 enum 里。"""
    with pytest.raises(ValidationError):
        _finding(risk_level=level)


@pytest.mark.parametrize("value", ["", None])
def test_empty_title_reason_and_quote_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        _finding(risk_title=value)
    with pytest.raises(ValidationError):
        _finding(reason=value)
    with pytest.raises(ValidationError):
        _finding(quote=value)


def test_negative_clause_index_is_rejected() -> None:
    """范围标签必须是合法的非负序号；上界要等 Agent 拿到 clauses 才知道（P9-3）。"""
    with pytest.raises(ValidationError):
        _finding(clause_index=-1)


@pytest.mark.parametrize("hint", [0, -1])
def test_occurrence_hint_must_be_one_based(hint: int) -> None:
    with pytest.raises(ValidationError):
        _finding(occurrence_hint=hint)


def test_suggestion_type_is_constrained() -> None:
    with pytest.raises(ValidationError):
        Suggestion(type="REWRITE", text="x")


def test_suggestion_reason_may_be_absent() -> None:
    assert Suggestion(type="DELETE", text="建议删除该条").reason is None


# --------------------------------------------------------------------------- #
# related_rule_code：只定义契约，不做运行时关联（P9-0 裁决）
# --------------------------------------------------------------------------- #
def test_related_rule_code_may_be_none() -> None:
    """不确定就留空 —— 留空是安全的选择。"""
    assert _finding(related_rule_code=None).related_rule_code is None


def test_related_rule_code_carries_a_code_when_present() -> None:
    assert _finding(related_rule_code="LIAB_UNLIMITED_001").related_rule_code == "LIAB_UNLIMITED_001"


# --------------------------------------------------------------------------- #
# 顶层输出对象
# --------------------------------------------------------------------------- #
def test_review_result_wraps_findings() -> None:
    """顶层必须是 **JSON 对象** —— ``response_format=json_object`` 不接受裸数组。"""
    result = LLMReviewResult(findings=[_finding()])

    assert len(result.findings) == 1
    assert LLMReviewResult.model_json_schema()["type"] == "object"


def test_review_result_may_be_empty() -> None:
    """没发现问题就返回空数组 —— 不做"凑数式"报告。"""
    assert LLMReviewResult().findings == []
    assert LLMReviewResult.model_json_schema()["properties"]["findings"]["type"] == "array"


# --------------------------------------------------------------------------- #
# 提示输入契约
# --------------------------------------------------------------------------- #
def test_clause_context_uses_agent_side_identifiers() -> None:
    """条款只带 ``clause_index``（Agent 坐标系），**没有** Backend 的 ``clause_id``。"""
    assert "clause_id" not in ClauseContext.model_fields
    assert (
        ClauseContext(
            clause_index=3, clause_type="AMOUNT_PAYMENT", clause_no="第三条", title="付款方式", text="……"
        ).clause_index
        == 3
    )


def test_clause_review_prompt_input_requires_at_least_one_clause() -> None:
    with pytest.raises(ValidationError):
        ClauseReviewPromptInput(contract_type="PURCHASE", clauses=[])


def test_clause_review_prompt_input_matched_rules_default_to_empty() -> None:
    payload = ClauseReviewPromptInput(
        contract_type="PURCHASE",
        clauses=[ClauseContext(clause_index=0, clause_type="OTHER", text="……")],
    )

    assert payload.matched_rules == []


def test_matched_rule_hint_requires_a_rule_code() -> None:
    """**rule_code 必填**：没有它，related_rule_code 就没有可选项可回填。"""
    with pytest.raises(ValidationError):
        MatchedRuleHint(rule_code="", rule_name="x", dimension="y", risk_level="HIGH", quote="z")


# --------------------------------------------------------------------------- #
# Schema 单一事实来源
# --------------------------------------------------------------------------- #
def test_json_schema_is_generated_from_the_model() -> None:
    schema = LLMFinding.model_json_schema()

    assert schema["required"] == [
        "clause_index",
        "dimension",
        "risk_title",
        "risk_level",
        "reason",
        "quote",
    ]
    assert schema["properties"]["risk_level"]["enum"] == ["HIGH", "MEDIUM", "LOW"]
    assert schema["properties"]["context_before"]["maxLength"] == 30, "长度上限必须出现在注入提示的 schema 里"
    assert schema["properties"]["context_after"]["maxLength"] == 30
    assert "paragraph_index" not in schema["properties"]


def test_injectable_schema_comes_from_the_same_model() -> None:
    """注入提示的那段由**同一个模型**产生 —— 不存在第二份手抄 schema。"""
    instruction = json_guard.build_schema_instruction(LLMFinding)

    assert '"risk_level"' in instruction
    assert '"related_rule_code"' in instruction
    assert '"paragraph_index"' not in instruction


def test_adding_a_field_flows_into_the_injected_schema() -> None:
    """加字段只需改模型 —— schema 与提示会自动跟上（这正是不手抄的理由）。"""

    class _Extended(LLMFinding):
        new_field: str | None = None

    assert "new_field" not in json_guard.build_schema_instruction(LLMFinding)
    assert "new_field" in json_guard.build_schema_instruction(_Extended)
