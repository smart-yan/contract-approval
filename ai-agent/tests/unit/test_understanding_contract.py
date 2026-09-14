"""字符串契约的防漂移测试。

``ClauseType`` / ``ExtractMethod`` 是 **Agent 侧复制的 Backend 契约**（见
``app/core/constants.py`` 的说明）。复制意味着可能漂移 —— 这个文件把取值集合钉死，
挡住"Agent 侧被误改"。

⚠️ **它挡不住"Backend 改了而 Agent 没跟"** —— 真正的跨服务漂移检查需要共享包或
跨仓测试，两者都超出当前范围。这是已知缺口，不是被这个文件解决的问题。
"""

from __future__ import annotations

import pytest

from app.core.constants import ClauseType, ExtractMethod
from app.schemas.understanding import Clause

#: 与 ``backend/app/core/constants.py`` 的 ClauseType 一一对应
EXPECTED_CLAUSE_TYPES = {
    "SUBJECT",
    "AMOUNT_PAYMENT",
    "ACCEPTANCE",
    "LIABILITY",
    "CONFIDENTIAL",
    "IP",
    "DISPUTE",
    "FORCE_MAJEURE",
    "DATA_SECURITY",
    "DELIVERY",
    "OTHER",
}

#: 与 ``backend/app/core/constants.py`` 的 ExtractMethod 一一对应
EXPECTED_EXTRACT_METHODS = {"RULE", "REGEX", "LLM", "MANUAL"}

#: Clause 的字段集合 —— P7-1 明确不加 char offsets / block_id / task_id /
#: contract_id / confidence / level / parent / page / risk / keyword
EXPECTED_CLAUSE_FIELDS = {
    "clause_index",
    "clause_no",
    "title",
    "clause_type",
    "start_paragraph_index",
    "end_paragraph_index",
    "text",
    "extract_method",
}


def test_clause_type_values_are_frozen() -> None:
    assert {member.value for member in ClauseType} == EXPECTED_CLAUSE_TYPES
    assert len(EXPECTED_CLAUSE_TYPES) == 11, "Backend 侧是 11 个类型"


def test_extract_method_values_are_frozen() -> None:
    assert {member.value for member in ExtractMethod} == EXPECTED_EXTRACT_METHODS
    assert len(EXPECTED_EXTRACT_METHODS) == 4


def test_clause_fields_are_frozen() -> None:
    """字段一旦悄悄增删，落库映射与前端契约都会跟着错位 —— 因此钉死。"""
    assert set(Clause.model_fields) == EXPECTED_CLAUSE_FIELDS


@pytest.mark.parametrize("forbidden", ["confidence", "char_start_global", "char_end_global", "block_id"])
def test_clause_does_not_carry_out_of_scope_fields(forbidden: str) -> None:
    assert forbidden not in Clause.model_fields


def test_clause_type_is_a_plain_string_contract() -> None:
    """``Clause.clause_type`` 是 ``str`` 而不是枚举 —— 它是跨服务的字符串契约。"""
    assert Clause.model_fields["clause_type"].annotation is str
    assert Clause.model_fields["extract_method"].annotation is str
