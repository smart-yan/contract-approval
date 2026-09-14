"""``rule_review`` 节点的职责：State → 遍历 → evaluate_rule → State。

只验证**编解码、遍历与三态保真**；规则怎么判断由 ``test_rule_evaluator.py`` 负责，
节点不重复实现它（见 ``test_node_delegates_to_the_evaluator``）。
"""

from __future__ import annotations

from importlib import import_module

from app.core.errors import AgentErrorCode
from app.graph.nodes.rule_review import rule_review
from app.rules.schemas import (
    AgentRule,
    EvaluationFailureReason,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleRisk,
    RuleSetSnapshot,
)
from app.schemas.understanding import Clause, MetadataItem
from app.understanding.clauses import identify_clauses
from tests.factories import make_parse_result, para

#: 与 backend/scripts/seed_rules.py 的 3 条规则同形（截短版本，见 test_rule_catalog.py 的说明）
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
LIAB_RULE = AgentRule(
    rule_code="LIAB_UNLIMITED_001",
    rule_name="我方单方承担无限责任",
    dimension="违约责任",
    rule_type="KEYWORD",
    expression={"keywords": ["全部损失"], "logic": "ANY"},
    target_clause_types=["LIABILITY"],
    severity="HIGH",
    sort_order=30,
)
THRESHOLD_RULE = AgentRule(
    rule_code="PAY_PREPAY_RATIO_001",
    rule_name="预付款比例超过 30%",
    dimension="金额支付",
    rule_type="THRESHOLD",
    expression={"field": "prepay_ratio", "op": "gt", "value": 0.3},
    target_clause_types=["AMOUNT_PAYMENT"],
    severity="MEDIUM",
    sort_order=50,
)

#: 文档里有 IP 命中、有 LIABILITY 条款但不含关键词 → 三条规则分别得到
#: MATCHED / NOT_MATCHED / EVALUATION_FAILED
DOCUMENT = (
    "第一条 知识产权",
    "本项目产生的知识产权归乙方所有。",
    "第二条 违约责任",
    "乙方应在 10 日内完成整改。",
)


def _clauses(*texts: str) -> list[Clause]:
    return identify_clauses(make_parse_result(*[para(text) for text in texts]))


def _snapshot(*rules: AgentRule, version: str | None = "v1") -> RuleSetSnapshot:
    return RuleSetSnapshot(contract_type="PURCHASE", rule_set_version=version, rules=list(rules))


#: 模块对象本身。⚠️ 不能写 ``monkeypatch.setattr("app.graph.nodes.rule_review.evaluate_rule", …)``：
#: ``nodes/__init__.py`` 重导出了同名函数，``app.graph.nodes.rule_review`` 这个名字
#: 在包命名空间里指的是**函数**而不是模块。
_RULE_REVIEW_MODULE = import_module("app.graph.nodes.rule_review")


# --------------------------------------------------------------------------- #
# PURCHASE + 3 条规则：正常执行
# --------------------------------------------------------------------------- #
def test_purchase_with_three_rules() -> None:
    state = {
        "file_id": 7,
        "rule_snapshot": _snapshot(IP_RULE, LIAB_RULE, THRESHOLD_RULE),
        "clauses": _clauses(*DOCUMENT),
    }

    updates = rule_review(state)

    assert set(updates) == {"rule_evaluations", "rule_risks"}, "只写这两个字段，不污染其它 State 字段"
    assert [e.rule_code for e in updates["rule_evaluations"]] == [
        "IP_OWNER_SUPPLIER_001",
        "LIAB_UNLIMITED_001",
        "PAY_PREPAY_RATIO_001",
    ]


def test_matched_result_is_preserved() -> None:
    updates = rule_review({"rule_snapshot": _snapshot(IP_RULE), "clauses": _clauses(*DOCUMENT)})

    (evaluation,) = updates["rule_evaluations"]
    assert evaluation.status is RuleEvaluationStatus.MATCHED
    assert [risk.risk_code for risk in updates["rule_risks"]] == ["IP_OWNER_SUPPLIER_001"]


def test_not_matched_result_is_preserved() -> None:
    updates = rule_review({"rule_snapshot": _snapshot(LIAB_RULE), "clauses": _clauses(*DOCUMENT)})

    (evaluation,) = updates["rule_evaluations"]
    assert evaluation.status is RuleEvaluationStatus.NOT_MATCHED
    assert evaluation.risks == []
    assert evaluation.failure_reason is None
    assert updates["rule_risks"] == []


# --------------------------------------------------------------------------- #
# EVALUATION_FAILED 不得被折叠
# --------------------------------------------------------------------------- #
def test_evaluation_failed_is_not_converted_to_not_matched() -> None:
    """THRESHOLD 缺 prepay_ratio（GAP-C）—— 结论必须停在"无法求值"。"""
    updates = rule_review({"rule_snapshot": _snapshot(THRESHOLD_RULE), "clauses": _clauses(*DOCUMENT)})

    (evaluation,) = updates["rule_evaluations"]
    assert evaluation.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert evaluation.status is not RuleEvaluationStatus.NOT_MATCHED
    assert evaluation.failure_reason is EvaluationFailureReason.MISSING_INPUT
    assert evaluation.failure_message, "失败原因必须能被人看到"
    assert updates["rule_risks"] == []


def test_evaluation_failed_is_not_dropped_from_the_result() -> None:
    """失败的规则**留在结果里**，不被跳过 —— 否则"少跑了一条"就没有痕迹。"""
    updates = rule_review(
        {"rule_snapshot": _snapshot(IP_RULE, THRESHOLD_RULE), "clauses": _clauses(*DOCUMENT)}
    )

    statuses = [e.status for e in updates["rule_evaluations"]]
    assert statuses == [RuleEvaluationStatus.MATCHED, RuleEvaluationStatus.EVALUATION_FAILED]
    assert len(updates["rule_evaluations"]) == 2, "算不出来的那条也在结果里"


def test_failed_rules_do_not_swallow_the_risks_of_others() -> None:
    updates = rule_review(
        {"rule_snapshot": _snapshot(IP_RULE, LIAB_RULE, THRESHOLD_RULE), "clauses": _clauses(*DOCUMENT)}
    )

    assert [risk.risk_code for risk in updates["rule_risks"]] == ["IP_OWNER_SUPPLIER_001"]


# --------------------------------------------------------------------------- #
# 空规则集 / 没有规则快照
# --------------------------------------------------------------------------- #
def test_empty_rule_set_runs_normally() -> None:
    """``rule_set_version=None, rules=[]`` 是**正常的业务结论**，不是错误。"""
    updates = rule_review({"rule_snapshot": _snapshot(), "clauses": _clauses(*DOCUMENT)})

    assert updates["rule_evaluations"] == []
    assert updates["rule_risks"] == []


def test_empty_rule_set_does_not_invent_risks() -> None:
    updates = rule_review({"rule_snapshot": _snapshot(), "clauses": _clauses(*DOCUMENT)})

    assert updates["rule_risks"] == []


def test_missing_rule_snapshot_is_a_workflow_failure() -> None:
    """规则快照没进 Workflow = **输入缺失的失败**，不是"没有规则"这种正常结论。"""
    updates = rule_review({"file_id": 7, "clauses": _clauses(*DOCUMENT)})

    assert updates["error_code"] == AgentErrorCode.AGENT_INPUT_INVALID.value
    assert "rule_snapshot" in updates["error_message"]


def test_missing_rule_snapshot_produces_no_evaluations_and_no_risks() -> None:
    """失败时**保持缺失**，而不是写成空列表 —— 空列表会被读成"跑了，0 条结论"。"""
    updates = rule_review({"clauses": _clauses(*DOCUMENT)})

    assert "rule_evaluations" not in updates
    assert "rule_risks" not in updates


def test_missing_rule_snapshot_is_never_dressed_up_as_not_matched() -> None:
    """没有任何结论，也就没有任何东西能被读成 NOT_MATCHED（= 合同没问题）。"""
    updates = rule_review({"clauses": _clauses(*DOCUMENT)})

    assert "rule_evaluations" not in updates
    assert not any(
        getattr(evaluation, "status", None) is RuleEvaluationStatus.NOT_MATCHED
        for evaluation in updates.get("rule_evaluations", [])
    )


def test_missing_rule_snapshot_does_not_run_the_evaluator(monkeypatch) -> None:
    """没拿到规则就**不要求值** —— 不能拿一份空的规则去跑出"没命中"。"""
    calls: list[str] = []

    def spy(rule: AgentRule, clauses: list[Clause], metadata: object = ()) -> RuleEvaluationResult:
        calls.append(rule.rule_code)
        raise AssertionError("不该被调用")

    monkeypatch.setattr(_RULE_REVIEW_MODULE, "evaluate_rule", spy)

    updates = rule_review({"clauses": _clauses(*DOCUMENT)})

    assert calls == []
    assert updates["error_code"] == AgentErrorCode.AGENT_INPUT_INVALID.value


def test_missing_rule_snapshot_differs_from_an_empty_rule_set() -> None:
    """一个写 error、一个不写；**绝不能都表现成"0 风险正常结束"**。"""
    clauses = _clauses(*DOCUMENT)
    empty_rule_set = _snapshot(version=None)

    without_snapshot = {"clauses": clauses} | rule_review({"clauses": clauses})
    with_empty_rule_set = {"clauses": clauses, "rule_snapshot": empty_rule_set} | rule_review(
        {"rule_snapshot": empty_rule_set, "clauses": clauses}
    )

    assert without_snapshot["error_code"] == AgentErrorCode.AGENT_INPUT_INVALID.value
    assert with_empty_rule_set.get("error_code") is None
    assert with_empty_rule_set["rule_evaluations"] == []
    assert with_empty_rule_set["rule_risks"] == []


def test_no_clauses_is_not_an_error() -> None:
    """没有条款（文档没切出条款）时按 P8-1 的契约得到 NOT_MATCHED，而不是崩溃。"""
    updates = rule_review({"rule_snapshot": _snapshot(IP_RULE)})

    (evaluation,) = updates["rule_evaluations"]
    assert evaluation.status is RuleEvaluationStatus.NOT_MATCHED


def test_empty_clauses_yield_no_risks() -> None:
    """上游因解析失败写了空 clauses —— 节点不报错，也不产出风险。"""
    updates = rule_review({"rule_snapshot": _snapshot(IP_RULE), "clauses": []})

    assert updates["rule_risks"] == []


# --------------------------------------------------------------------------- #
# 顺序与定位信息
# --------------------------------------------------------------------------- #
def test_rule_order_follows_the_snapshot() -> None:
    """顺序 = ``snapshot.rules`` 顺序，节点不排序、不筛选。"""
    snapshot = _snapshot(THRESHOLD_RULE, IP_RULE, LIAB_RULE)

    updates = rule_review({"rule_snapshot": snapshot, "clauses": _clauses(*DOCUMENT)})

    assert [e.rule_code for e in updates["rule_evaluations"]] == [rule.rule_code for rule in snapshot.rules]


def test_risk_order_follows_the_evaluations() -> None:
    clauses = _clauses(
        "第一条 知识产权",
        "知识产权归乙方所有。",
        "第二条 知识产权",
        "知识产权归乙方所有，甲方不得使用。",
    )

    updates = rule_review({"rule_snapshot": _snapshot(IP_RULE), "clauses": clauses})

    assert [risk.paragraph_index for risk in updates["rule_risks"]] == [1, 3]


def test_locator_fields_are_not_lost() -> None:
    """``paragraph_index`` / ``quote`` / ``original_text`` 等定位信息原样保留。"""
    clauses = _clauses(*DOCUMENT)
    paragraph_text = clauses[0].text.split("\n")[1]

    updates = rule_review({"rule_snapshot": _snapshot(IP_RULE), "clauses": clauses})

    (risk,) = updates["rule_risks"]
    assert risk.paragraph_index == 1
    assert risk.quote == "知识产权归乙方"
    assert risk.original_text == paragraph_text
    assert risk.quote in risk.original_text
    assert risk.source == "RULE"
    assert risk.risk_level == "HIGH"
    assert risk.dimension == "知识产权"
    assert risk.legal_basis is None


# --------------------------------------------------------------------------- #
# metadata 透传（P8-3）
# --------------------------------------------------------------------------- #
PREPAY_METADATA = MetadataItem(
    field_key="prepay_ratio",
    field_label="预付款比例",
    field_value="0.3",
    value_type="RATIO",
    paragraph_index=18,
    quote="1. 预付款\t30%\t合同生效后五（5）个工作日内支付",
    extract_method="REGEX",
)


def test_node_passes_state_metadata_to_the_evaluator(monkeypatch) -> None:
    """节点把 State 里的 metadata **原样**递下去 —— 它自己不解析、不转换、不筛选。"""
    captured: list[object] = []
    real_evaluate = _RULE_REVIEW_MODULE.evaluate_rule

    def spy(rule: AgentRule, clauses: list[Clause], metadata: object = ()) -> RuleEvaluationResult:
        captured.append(metadata)
        return real_evaluate(rule, clauses, metadata=metadata)

    monkeypatch.setattr(_RULE_REVIEW_MODULE, "evaluate_rule", spy)

    updates = rule_review(
        {
            "rule_snapshot": _snapshot(THRESHOLD_RULE),
            "clauses": _clauses(*DOCUMENT),
            "metadata": [PREPAY_METADATA],
        }
    )

    assert captured == [[PREPAY_METADATA]]
    assert captured[0] is captured[0], "递下去的是 State 里那一个对象，不是复制品"
    (evaluation,) = updates["rule_evaluations"]
    assert evaluation.status is RuleEvaluationStatus.NOT_MATCHED, "0.3 不超过 0.3"


def test_threshold_rule_gets_a_definite_answer_when_metadata_is_present() -> None:
    """有值可算时，THRESHOLD 从"无法求值"变成**确定结论**；风险定位来自 metadata。"""
    over_limit = PREPAY_METADATA.model_copy(update={"field_value": "0.5"})

    updates = rule_review(
        {
            "rule_snapshot": _snapshot(THRESHOLD_RULE),
            "clauses": _clauses(*DOCUMENT),
            "metadata": [over_limit],
        }
    )

    (evaluation,) = updates["rule_evaluations"]
    assert evaluation.status is RuleEvaluationStatus.MATCHED
    (risk,) = updates["rule_risks"]
    assert risk.paragraph_index == 18
    assert risk.quote == over_limit.quote
    assert risk.risk_code == "PAY_PREPAY_RATIO_001"


def test_metadata_absent_still_stops_at_missing_input() -> None:
    """没有 metadata（P7-2 没抽到该字段）时结论**一个字没变**：仍是 MISSING_INPUT。"""
    updates = rule_review({"rule_snapshot": _snapshot(THRESHOLD_RULE), "clauses": _clauses(*DOCUMENT)})

    (evaluation,) = updates["rule_evaluations"]
    assert evaluation.status is RuleEvaluationStatus.EVALUATION_FAILED
    assert evaluation.failure_reason is EvaluationFailureReason.MISSING_INPUT


# --------------------------------------------------------------------------- #
# 节点不实现求值逻辑
# --------------------------------------------------------------------------- #
def test_node_delegates_to_the_evaluator(monkeypatch) -> None:
    """节点必须**调用** ``evaluate_rule``，而不是自己判断规则是否命中。

    把求值器替换成一个只记录调用的哨兵：如果节点里藏着自己的匹配逻辑，
    它就绕不过这个哨兵，结果也就不会是哨兵造出来的那条风险。
    """
    calls: list[tuple[str, list[Clause]]] = []
    sentinel_risk = RuleRisk(
        risk_code="SENTINEL",
        risk_title="哨兵",
        dimension="哨兵",
        risk_level="LOW",
        reason="只有被调用才会出现",
        original_text="哨兵原文",
        paragraph_index=99,
        quote="哨兵",
    )

    def fake_evaluate(rule: AgentRule, clauses: list[Clause], metadata: object = ()) -> RuleEvaluationResult:
        calls.append((rule.rule_code, list(clauses)))
        return RuleEvaluationResult(
            rule_code=rule.rule_code,
            rule_name=rule.rule_name,
            status=RuleEvaluationStatus.MATCHED,
            risks=[sentinel_risk],
        )

    monkeypatch.setattr(_RULE_REVIEW_MODULE, "evaluate_rule", fake_evaluate)

    clauses = _clauses(*DOCUMENT)
    updates = rule_review({"rule_snapshot": _snapshot(IP_RULE, LIAB_RULE), "clauses": clauses})

    assert [code for code, _ in calls] == ["IP_OWNER_SUPPLIER_001", "LIAB_UNLIMITED_001"]
    assert all(passed == clauses for _, passed in calls), "条款原样传给求值器"
    assert updates["rule_risks"] == [sentinel_risk, sentinel_risk]
    assert all(risk is sentinel_risk for risk in updates["rule_risks"]), (
        "节点**不重新包装** RuleRisk —— 原对象直接进 State，字段不可能在搬运中丢失"
    )
