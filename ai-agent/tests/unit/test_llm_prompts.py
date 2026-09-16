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
    PROMPT_CLAUSE_REVIEW_V3,
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


# --------------------------------------------------------------------------- #
# v2：因为输出契约多了 dimension，提示必须升级（P9-8a）
# --------------------------------------------------------------------------- #
def test_current_version_is_v3() -> None:
    """输出契约变了，提示版本必须跟着变 —— 否则"历史任务用的哪版提示"就说不清。

    v3 的契约变化：``context_before`` / ``context_after`` 由「必须输出」改为
    「可选，默认省略」（P14-3-5，依据 P14-3-2/3-4 的真实调用数据）。
    """
    from app.llm.clause_review import build_clause_review_request

    payload = ClauseReviewPromptInput(contract_type="PURCHASE", clauses=CLAUSES)

    assert PROMPT_CLAUSE_REVIEW_V3 == "clause_review.v3"
    assert build_clause_review_request(payload).prompt_version == "clause_review.v3"


def test_v1_is_kept_but_documented_as_incompatible() -> None:
    """v1 **不删**（历史留痕），但它是跑不通的：正文里没提 dimension，模型不会输出它。"""
    from app.llm.prompts import PROMPT_CLAUSE_REVIEW_V1

    v1 = load_prompt(PROMPT_CLAUSE_REVIEW_V1)

    assert v1, "旧版本要留着"
    assert "dimension" not in v1, "v1 里没有 dimension 的说明 —— 因此它与新契约不兼容"


@pytest.mark.parametrize(
    ("constraint", "why"),
    [
        ("dimension", "必须提到这个字段"),
        ("审查维度", "必须说明它是**风险所属的审查维度**"),
        ("条款类型", "必须说明它与条款类型**不是一回事**"),
        ("固定候选集合", "必须说明只能从固定候选集合里选"),
        ("不要创造", "必须明令禁止创造集合外的取值"),
    ],
)
def test_current_prompt_states_the_dimension_rules(constraint: str, why: str) -> None:
    assert constraint in load_prompt(PROMPT_CLAUSE_REVIEW_V3), why


def test_current_prompt_points_at_the_schema_for_the_candidate_set() -> None:
    """候选集合**不在提示里抄一遍** —— 指向注入的 JSON Schema 的枚举。

    抄一份列表就是第二份真相源：schema 改了、抄本没改，而模型会照着错的那份选。
    """
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V3)
    from app.llm.findings import RiskDimensionLiteral

    assert "JSON Schema" in text and "枚举" in text
    assert '"enum"' not in text, "不许把 schema 片段抄进来"

    # 「抄一份列表」的形态：同一行里排出**三个以上**维度名。
    # （正文里单独用某个维度的词来解释含义是正常的，不算抄列表。）
    for line in text.splitlines():
        present = [d for d in RiskDimensionLiteral.__args__ if d in line]
        assert len(present) < 3, f"这一行列了 {len(present)} 个候选值，像是把枚举抄进来了：{line}"


def test_current_prompt_does_not_hand_the_model_a_near_miss_value() -> None:
    """⚠️ 提示里**不许出现"看起来像取值、其实不是"**的词。

    写提示时踩过一次：原本的例子写成「例如：知识产权归属、违约责任的轻重」——
    那读起来就是一份可选值清单，模型照着回一个"知识产权归属"，校验直接拒整批。
    """
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V3)
    from app.llm.findings import RiskDimensionLiteral

    for dimension in RiskDimensionLiteral.__args__:
        # 维度全称不能作为**独立词条**出现在括号举例里（`例如：X、Y`）那种形态
        assert f"例如：{dimension}" not in text
        assert f"、{dimension}、" not in text


def test_current_prompt_forbids_forcing_a_dimension() -> None:
    """必填字段**不构成**「必须报点什么」的压力（P9-8a 架构审查的返工点）。

    原先的正文写的是「填不出合适的维度时，选一个语义上最接近的候选值」——
    那是在证据不足时鼓励模型强行归类，与「dimension 不许猜、不许兜底」的口径冲突。
    必填字段的正确逃生口是**不报这条 finding**，而不是硬填一个。
    """
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V3)

    assert "最接近" not in text, "不许再出现「挑一个最接近的」这类措辞"
    assert "不要报这条" in text, "必须给出逃生口：证据支持不了任何候选维度时不报"
    assert "硬填" in text, "要点明：必填 ≠ 可以硬填"


def test_current_prompt_binds_the_choice_to_the_evidence() -> None:
    """选择依据只能是**证据**，不能是关键词、字面相似或条款类型。"""
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V3)

    assert "风险证据本身" in text, "必须把选择依据钉在证据上"
    for excuse in ("字面相似", "条款类型"):
        assert excuse in text, f"必须点名禁止这种凑法：{excuse}"


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

# --------------------------------------------------------------------------- #
# context：**可选，默认省略**（P14-3-5）
#
# 为什么改：真实 DeepSeek 调用（P14-3-2 七条 / P14-3-4 五条，共 6 条可验证样本）证明
# 模型给出的 context **全部**取自相邻的另一个段落，而定位器要求"同一段落内、紧贴
# quote 的字符级前后缀"。6/6 条：提供 context 把定位从 CLAUSE_SCOPED **降级**为
# CLAUSE_FALLBACK；不提供则全部精确命中。因此 prompt 不再要求模型生成它。
# --------------------------------------------------------------------------- #
def test_current_prompt_marks_context_as_optional() -> None:
    """v3 必须明说 context 是**可选**、且**默认不要输出**。"""
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V3)

    assert "可选字段" in text, "必须点明它是可选字段"
    assert "默认不要输出" in text, "必须给出默认动作：不输出"


def test_current_prompt_no_longer_demands_a_context_length_bound() -> None:
    """旧契约里"必须逐字复制、各不超过 30 字"这句**必须消失** —— 它要求模型生成 context。"""
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V3)

    assert "各不超过 30 字" not in text
    assert "同样必须逐字复制" not in text, "不能再把 context 列进『必须』"


def test_current_prompt_states_the_three_conditions_for_providing_context() -> None:
    """若模型仍要提供，必须同时满足三条 —— 这正是定位器的实际判据。"""
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V3)

    assert "同一个段落" in text, "条件①：同段"
    assert "紧贴" in text, "条件②：字符级紧邻"
    assert "逐字复制" in text, "条件③：逐字"


def test_current_prompt_forbids_borrowing_a_neighbouring_paragraph() -> None:
    """必须点名禁止"引用相邻的另一个段落" —— 这正是实测中模型**每次**都犯的错。"""
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V3)

    assert "相邻的另一个段落" in text
    assert "降级" in text, "要说清后果：填错会把定位降级（而不是仅仅『没用』）"


def test_current_prompt_says_quote_is_the_primary_evidence() -> None:
    """``quote`` 是主要定位证据；context 缺失不是失败。"""
    text = load_prompt(PROMPT_CLAUSE_REVIEW_V3)

    assert "主要证据" in text
    assert "留空是正确的做法" in text, "必须给出逃生口：没有合格上下文时留空是对的"


def test_v2_is_kept_but_no_longer_current() -> None:
    """v2 **不删**（历史留痕）—— 它记录了"曾经要求模型生成 context"那一版契约。

    与 v1 同一条规矩：覆盖旧文件会让"历史任务当时用的哪版提示"永远查不回来。
    """
    from app.llm.prompts import PROMPT_CLAUSE_REVIEW_V2

    v2 = load_prompt(PROMPT_CLAUSE_REVIEW_V2)

    assert v2, "旧版本要留着"
    assert "各不超过 30 字" in v2, "v2 里那句『必须生成 context』必须原样保留 —— 它是历史证据"
    assert PROMPT_CLAUSE_REVIEW_V2 != PROMPT_CLAUSE_REVIEW_V3
