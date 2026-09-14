"""``prompts`` —— 版本化提示文本与渲染（P9-2）。

提示是**业务契约的一部分**，因此它的关键约束也要被测试钉住：
少了"不得编造条款"这条，模型就会开始引用不存在的条款，而**没有任何代码会报错**。
"""

from __future__ import annotations

import pytest

from app.llm.findings import ClauseContext, ClauseReviewPromptInput, MatchedRuleHint
from app.llm.json_guard import build_schema_instruction
from app.llm.prompts import (
    PROMPT_CLAUSE_REVIEW_V1,
    load_prompt,
    render_clause_review_user_prompt,
)

CLAUSES = [
    ClauseContext(
        clause_index=3,
        clause_type="AMOUNT_PAYMENT",
        clause_no="第三条",
        title="付款方式",
        text="3.1 双方约定的付款计划如下：\n1. 预付款 30% 合同生效后支付",
    ),
    ClauseContext(
        clause_index=4, clause_type="IP", clause_no="第四条", title="知识产权", text="知识产权归乙方所有。"
    ),
]

MATCHED = [
    MatchedRuleHint(
        rule_code="PAY_PREPAY_RATIO_001",
        rule_name="预付款比例超过 30%",
        dimension="金额支付",
        risk_level="MEDIUM",
        quote="1. 预付款\t30%",
    )
]


# --------------------------------------------------------------------------- #
# 提示文件
# --------------------------------------------------------------------------- #
def test_version_is_the_file_name() -> None:
    assert PROMPT_CLAUSE_REVIEW_V1 == "clause_review.v1"


def test_prompt_file_is_loadable_and_non_empty() -> None:
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V1)

    assert text
    assert "合同" in text


@pytest.mark.parametrize(
    ("constraint", "why"),
    [
        ("编造", "不得编造条款 —— 少了它模型会引用不存在的条款"),
        ("逐字", "quote 必须逐字复制 —— 它是定位与人工核对的唯一依据"),
        ("related_rule_code", "必须写明该字段只能从规则清单里选"),
        ("clause_index", "必须写明编号要原样回显"),
        ("不得", "约束必须以禁令形式写清楚"),
    ],
)
def test_prompt_states_the_hard_constraints(constraint: str, why: str) -> None:
    assert constraint in load_prompt(PROMPT_CLAUSE_REVIEW_V1), why


def test_prompt_tells_the_model_not_to_emit_coordinates() -> None:
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V1)

    assert "坐标" in text
    assert "paragraph_index" not in text, "不该向模型提起一个它不用输出的字段"


def test_prompt_does_not_hand_copy_a_json_schema() -> None:
    """提示文件里**不抄 schema** —— 抄本一定会与真正校验的模型漂移。

    （提示里**提到** ``risk_level`` 是业务口径 —— "等级只能取 HIGH/MEDIUM/LOW"；
    这里禁止的是**抄一份结构定义**：JSON Schema 的关键字一个都不许出现。）
    """
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V1)

    assert '"properties"' not in text
    assert '"$defs"' not in text
    assert '"type": "object"' not in text


def test_unknown_prompt_version_fails_loudly() -> None:
    """版本不存在是**打包/部署错误** —— 必须响亮失败，不能回落成空提示。"""
    with pytest.raises(FileNotFoundError):
        load_prompt("clause_review.v99")


def test_business_rules_stay_out_of_the_schema_instruction() -> None:
    """反过来也要成立：机械注入里不掺业务口径（职责不串味）。"""
    instruction = build_schema_instruction(ClauseReviewPromptInput)

    assert "编造" not in instruction
    assert "逐字" not in instruction


# --------------------------------------------------------------------------- #
# user prompt 渲染
# --------------------------------------------------------------------------- #
def test_renders_contract_type_and_clauses() -> None:
    text = render_clause_review_user_prompt(
        ClauseReviewPromptInput(contract_type="PURCHASE", clauses=CLAUSES)
    )

    assert "PURCHASE" in text
    assert "条款 #3" in text
    assert "条款 #4" in text
    assert "第三条 付款方式·AMOUNT_PAYMENT" in text
    assert CLAUSES[0].text in text, "条款全文必须原样出现（模型只能依据它判断）"


def test_renders_rule_codes_for_related_rule_code() -> None:
    """**rule_code 必须出现在输入里** —— 否则 related_rule_code 无值可选，模型只能编。"""
    text = render_clause_review_user_prompt(
        ClauseReviewPromptInput(contract_type="PURCHASE", clauses=CLAUSES, matched_rules=MATCHED)
    )

    assert "PAY_PREPAY_RATIO_001" in text
    assert "预付款比例超过 30%" in text
    assert "related_rule_code" in text


def test_renders_without_matched_rules() -> None:
    text = render_clause_review_user_prompt(
        ClauseReviewPromptInput(contract_type="PURCHASE", clauses=CLAUSES)
    )

    assert "（无）" in text
    assert "rule_code=" not in text


def test_rendered_prompt_contains_no_coordinates() -> None:
    text = render_clause_review_user_prompt(
        ClauseReviewPromptInput(contract_type="PURCHASE", clauses=CLAUSES, matched_rules=MATCHED)
    )

    assert "paragraph_index" not in text
    assert "char_start" not in text


def test_rendering_is_deterministic() -> None:
    payload = ClauseReviewPromptInput(contract_type="PURCHASE", clauses=CLAUSES, matched_rules=MATCHED)

    assert render_clause_review_user_prompt(payload) == render_clause_review_user_prompt(payload)


def test_prompt_input_has_no_data_sourceless_role_field() -> None:
    """我方立场**没有数据源**（Backend 只有 our_party / counterparty 主体名称），
    因此契约里不留幽灵字段 —— 审查视角只由 contract_type 决定。"""
    assert "our_party_role" not in ClauseReviewPromptInput.model_fields


def test_rendered_prompt_has_no_role_label() -> None:
    text = render_clause_review_user_prompt(
        ClauseReviewPromptInput(contract_type="PURCHASE", clauses=CLAUSES)
    )

    assert "我方立场" not in text
    assert "【合同类型】PURCHASE" in text
