"""风险持久化的单元测试（**不需要 MySQL**）。

这里只测两件在数据库之外就能钉死的事：

1. 请求契约**不允许**客户端指定服务端字段（``review_status`` / ``locator_type`` /
   ``rule_id`` / ``clause_id``）—— 这是"谁来决定这些值"的边界，不是风格问题
2. 段落号 → 条款的判定是**纯函数**，三种结局（命中一个 / 一个都没有 / 多个同时命中）
   各自的行为可以直接测

真正的落库行为（事务、幂等、外键解析）在
``tests/integration/test_risk_api.py`` 里，需要真实 MySQL。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.constants import RiskLevel, RiskSource
from app.core.errors import ValidationError as AppValidationError
from app.schemas.risk import RiskItemCreate, RiskPersistRequest
from app.services.risk_persistence import _ClauseRange, _resolve_clause_id


# --------------------------------------------------------------------------- #
# 请求契约：哪些字段客户端**不能**指定
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "forbidden",
    ["review_status", "locator_type", "rule_id", "clause_id", "task_id", "contract_id", "id"],
)
def test_the_request_cannot_carry_server_side_fields(forbidden: str) -> None:
    """这些字段要么由服务端固定、要么由服务端解析。

    ``review_status`` 尤其关键：它能让客户端把一条 AI 风险标成"法务已确认"。
    """
    assert forbidden not in RiskItemCreate.model_fields


def test_the_request_has_exactly_the_agreed_fields() -> None:
    assert set(RiskItemCreate.model_fields) == {
        "risk_code",
        "risk_title",
        "dimension",
        "risk_level",
        "source",
        "reason",
        "legal_basis",
        "quote",
        "paragraph_index",
        "anchor_method",
    }


def test_the_field_is_named_quote_not_original_text() -> None:
    """⚠️ 名字是契约的一部分。

    ``risk_item.original_text`` 列要的是**命中片段**，而 Agent 的
    ``AgentRiskItem.original_text`` 是**段落原文**。请求字段叫 ``quote``，
    就是为了让"按名字对拷"这件事不可能发生。
    """
    assert "quote" in RiskItemCreate.model_fields
    assert "original_text" not in RiskItemCreate.model_fields


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "risk_title": "知识产权归属相对方",
        "dimension": "知识产权",
        "risk_level": "HIGH",
        "source": "RULE",
        "quote": "知识产权归乙方",
        "paragraph_index": 23,
    }
    payload.update(overrides)
    return payload


def test_a_minimal_payload_is_valid() -> None:
    item = RiskItemCreate(**_payload())

    assert item.risk_level is RiskLevel.HIGH
    assert item.source is RiskSource.RULE
    assert item.risk_code is None, "纯 LLM 风险没有规则编码"
    assert item.anchor_method is None


@pytest.mark.parametrize("bad", ["URGENT", "HIGH ", ""])
def test_an_unknown_risk_level_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        RiskItemCreate(**_payload(risk_level=bad))


@pytest.mark.parametrize("bad", ["RULE+llm", "RULE_AND_LLM", "OTHER"])
def test_an_unknown_source_is_rejected(bad: str) -> None:
    """``RULE+LLM`` 是唯一合法写法（与架构文档 §7.2 一致）。"""
    with pytest.raises(ValidationError):
        RiskItemCreate(**_payload(source=bad))


def test_the_merged_source_is_accepted() -> None:
    assert RiskItemCreate(**_payload(source="RULE+LLM")).source is RiskSource.RULE_AND_LLM


@pytest.mark.parametrize(
    ("field", "bad"),
    [("quote", ""), ("risk_title", ""), ("dimension", ""), ("paragraph_index", -1)],
)
def test_empty_evidence_and_invalid_coordinates_are_rejected(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        RiskItemCreate(**_payload(**{field: bad}))


def test_an_empty_batch_is_allowed() -> None:
    """没有风险也是一次真实结论 —— 允许空列表，好让任务能被正常结束。"""
    assert RiskPersistRequest(risks=[]).risks == []


# --------------------------------------------------------------------------- #
# 段落号 → 条款
# --------------------------------------------------------------------------- #
_RANGES = (_ClauseRange(clause_id=7, paragraph_start=10, paragraph_end=20),)


def test_a_covered_paragraph_resolves_to_its_clause() -> None:
    assert _resolve_clause_id(_RANGES, 10) == 7, "闭区间：起点算在内"
    assert _resolve_clause_id(_RANGES, 15) == 7
    assert _resolve_clause_id(_RANGES, 20) == 7, "终点也算在内"


def test_an_uncovered_paragraph_resolves_to_none() -> None:
    """**没有对应条款**是事实，不是猜测 —— ``clause_id`` 本来就 nullable（§7.2）。"""
    assert _resolve_clause_id(_RANGES, 9) is None
    assert _resolve_clause_id(_RANGES, 21) is None


def test_no_clauses_at_all_resolves_to_none() -> None:
    """⚠️ 当前是**常态**：``clause`` / ``document_block`` 还没有任何写入路径。

    条款切分在 Agent 侧，落库尚未实现 —— 因此每个风险都会走到这里。
    它不是异常，也不该让整批失败。
    """
    assert _resolve_clause_id((), 23) is None


def test_the_extension_is_normalized_before_choosing_a_locator() -> None:
    """``.DOCX`` / ``docx`` 是同一件事 —— 派生定位方式时不能因为大小写或前导点选错分支。"""
    from app.services.risk_persistence import _normalize_ext

    assert _normalize_ext(".DOCX") == "docx"
    assert _normalize_ext(" PDF ") == "pdf"
    assert _normalize_ext("docx") == "docx"


def test_an_ambiguous_paragraph_is_rejected() -> None:
    """两个条款覆盖同一段说明坐标数据有问题 —— 此时挑任何一个都是**猜**。"""
    overlapping = (
        _ClauseRange(clause_id=7, paragraph_start=10, paragraph_end=20),
        _ClauseRange(clause_id=8, paragraph_start=18, paragraph_end=25),
    )

    with pytest.raises(AppValidationError) as excinfo:
        _resolve_clause_id(overlapping, 19)

    assert excinfo.value.details["candidate_clause_ids"] == [7, 8]
