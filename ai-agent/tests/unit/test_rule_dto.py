"""Agent 规则契约（P8-1）：Backend → Agent 映射 + 字段集合防漂移。

与 ``test_understanding_contract.py`` 同一路子 —— 那个文件钉的是 P7 的契约，
这个文件钉的是 P8 的：**Agent 的规则模型里"不该有的是什么"**。

重点不是"能不能映射过去"，而是"映射过去以后**多没多、少没少**"：
数据库身份字段混进来不会让任何功能立刻坏掉，它只会让 Agent 悄悄依赖上
Backend 的内部标识 —— 所以要用测试钉死。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.constants import RuleType
from app.rules.schemas import (
    AgentRule,
    EvaluationFailureReason,
    RuleRisk,
    RuleSetSnapshot,
    rule_from_backend,
)

#: 与 ``backend/app/schemas/rule.py::RuleItem`` 同形的一条规则（含 Backend 的 ``id``）
BACKEND_RULE_ITEM = {
    "id": 3,
    "rule_code": "IP_OWNER_SUPPLIER_001",
    "rule_name": "知识产权归属相对方",
    "dimension": "知识产权",
    "description": "采购场景下，若约定成果的知识产权归供方所有，我方将无法自由使用。",
    "rule_type": "KEYWORD",
    "expression": {"keywords": ["知识产权归供方"], "logic": "ANY"},
    "target_clause_types": ["IP"],
    "severity": "HIGH",
    "legal_basis": "《民法典》第 843 条",
    "suggestion_template": "建议修改为：知识产权归甲方所有。",
    "sort_order": 10,
}

#: AgentRule 的字段集合 —— 与 Backend RuleItem 一一对应，**去掉 id**
EXPECTED_AGENT_RULE_FIELDS = {
    "rule_code",
    "rule_name",
    "dimension",
    "description",
    "rule_type",
    "expression",
    "target_clause_types",
    "severity",
    "legal_basis",
    "suggestion_template",
    "sort_order",
}

#: RuleRisk 的字段集合 —— 就是 P8-1 的最小风险契约
EXPECTED_RULE_RISK_FIELDS = {
    "risk_code",
    "risk_title",
    "dimension",
    "risk_level",
    "source",
    "reason",
    "legal_basis",
    "original_text",
    "paragraph_index",
    "quote",
}

#: 这些名字**永远不该**出现在 Agent 的规则 / 风险结果里（属于持久化与人工审核层）
FORBIDDEN_FIELDS = {
    "id",
    "rule_set_id",
    "risk_item_id",
    "task_id",
    "contract_id",
    "clause_id",
    "block_id",
    "char_start",
    "char_end",
    "review_status",
    "confidence",
}


# --------------------------------------------------------------------------- #
# 字段集合
# --------------------------------------------------------------------------- #
def test_agent_rule_fields_are_frozen() -> None:
    assert set(AgentRule.model_fields) == EXPECTED_AGENT_RULE_FIELDS


def test_rule_risk_fields_are_frozen() -> None:
    assert set(RuleRisk.model_fields) == EXPECTED_RULE_RISK_FIELDS


@pytest.mark.parametrize("model", [AgentRule, RuleRisk])
def test_no_database_identity_or_persistence_fields(model) -> None:
    assert not (set(model.model_fields) & FORBIDDEN_FIELDS)


def test_rule_type_values_are_frozen() -> None:
    """Agent 侧认识的 rule_type 与 ``backend/app/core/constants.py`` 一一对应。"""
    assert {member.value for member in RuleType} == {"KEYWORD", "REGEX", "EXISTS", "MISSING", "THRESHOLD"}


def test_failure_reason_values_are_frozen() -> None:
    """``EVALUATION_FAILED`` 的原因集合 —— 每种原因对应一种不同的后续动作。"""
    assert {member.value for member in EvaluationFailureReason} == {
        "UNSUPPORTED_RULE_TYPE",
        "INVALID_EXPRESSION",
        "MISSING_INPUT",
        "INVALID_TARGET_CLAUSE_TYPE",
    }


# --------------------------------------------------------------------------- #
# Backend → Agent 映射
# --------------------------------------------------------------------------- #
def test_maps_every_agent_field() -> None:
    rule = rule_from_backend(BACKEND_RULE_ITEM)

    for name in EXPECTED_AGENT_RULE_FIELDS:
        assert getattr(rule, name) == BACKEND_RULE_ITEM[name], name


def test_backend_database_id_is_dropped() -> None:
    """``id`` 是 Backend 的资源标识，不是 Agent 的字段。"""
    rule = rule_from_backend(BACKEND_RULE_ITEM)

    assert not hasattr(rule, "id")


def test_unknown_backend_fields_are_ignored() -> None:
    """Backend 将来新增字段时，不能自动流进 Agent 的核心模型。"""
    payload = {**BACKEND_RULE_ITEM, "created_at": "2026-01-01T00:00:00", "rule_set_id": 1}

    rule = rule_from_backend(payload)

    assert not hasattr(rule, "created_at")
    assert not hasattr(rule, "rule_set_id")


def test_mapping_does_not_mutate_the_payload() -> None:
    payload = {**BACKEND_RULE_ITEM, "expression": {"keywords": ["知识产权归供方"], "logic": "ANY"}}
    snapshot = {k: v for k, v in payload.items()}

    rule = rule_from_backend(payload)

    assert payload == snapshot
    assert rule.expression == snapshot["expression"]


def test_optional_fields_may_be_absent() -> None:
    """``description`` / ``legal_basis`` / ``suggestion_template`` / ``target_clause_types``
    在 Backend 侧是可空的（契约如此），映射后同样可以为空。"""
    payload = {
        key: value
        for key, value in BACKEND_RULE_ITEM.items()
        if key not in {"description", "legal_basis", "suggestion_template", "target_clause_types"}
    }

    rule = rule_from_backend(payload)

    assert rule.description is None
    assert rule.legal_basis is None
    assert rule.suggestion_template is None
    assert rule.target_clause_types is None


def test_missing_required_field_is_a_contract_violation() -> None:
    """少给必填字段是**契约被破坏**，不是可预期的业务结果 —— 不静默兜底。"""
    payload = {k: v for k, v in BACKEND_RULE_ITEM.items() if k != "rule_code"}

    with pytest.raises(ValidationError):
        rule_from_backend(payload)


def test_unknown_rule_type_is_accepted_by_the_dto() -> None:
    """``rule_type`` 是**字符串**：Backend 配了一个 Agent 不认识的类型，
    规则要能构造出来，由求值器给出"无法求值" —— 而不是在加载阶段炸掉整份规则集。"""
    rule = rule_from_backend({**BACKEND_RULE_ITEM, "rule_type": "SEMANTIC"})

    assert rule.rule_type == "SEMANTIC"


def test_expression_is_carried_verbatim() -> None:
    """expression 的解释权在求值器，DTO 层不做归一化/枚举化。"""
    expression = {"field": "prepay_ratio", "op": "gt", "value": 0.3}

    rule = rule_from_backend({**BACKEND_RULE_ITEM, "rule_type": "THRESHOLD", "expression": expression})

    assert rule.expression == expression
    assert rule.dimension == "知识产权", "dimension 原样搬运，不映射成枚举码"


# --------------------------------------------------------------------------- #
# RuleSetSnapshot 的领域不变量
#
# 直接构造 DTO 也要挡住自相矛盾的快照 —— 不变量属于**对象**，不只是映射函数的产物。
# --------------------------------------------------------------------------- #
def test_snapshot_without_rule_set_must_have_no_rules() -> None:
    with pytest.raises(ValidationError):
        RuleSetSnapshot(
            contract_type="PURCHASE",
            rule_set_version=None,
            rules=[rule_from_backend(BACKEND_RULE_ITEM)],
        )


def test_snapshot_without_rule_set_and_without_rules_is_valid() -> None:
    snapshot = RuleSetSnapshot(contract_type="SERVICE", rule_set_version=None, rules=[])

    assert snapshot.rules == []


def test_snapshot_version_must_not_be_an_empty_string() -> None:
    """有规则集就有版本 —— 空字符串不是"有版本"。"""
    with pytest.raises(ValidationError):
        RuleSetSnapshot(contract_type="PURCHASE", rule_set_version="", rules=[])


@pytest.mark.parametrize("rule_count", [0, 1])
def test_snapshot_with_version_accepts_any_rule_count(rule_count: int) -> None:
    """有版本时 0 条与 N 条都合法（规则集存在，只是可能没配规则）。"""
    rules = [rule_from_backend(BACKEND_RULE_ITEM)] * rule_count

    snapshot = RuleSetSnapshot(contract_type="PURCHASE", rule_set_version="v1", rules=rules)

    assert len(snapshot.rules) == rule_count


# --------------------------------------------------------------------------- #
# source 是类型约束，不是口头约定
# --------------------------------------------------------------------------- #
def test_risk_source_is_locked_to_rule() -> None:
    with pytest.raises(ValidationError):
        RuleRisk(
            risk_code="X",
            risk_title="X",
            dimension="X",
            risk_level="HIGH",
            source="LLM",
            reason="X",
            original_text="X",
            paragraph_index=0,
            quote="X",
        )
