"""``has_usable_document`` —— "解析这一步算不算成功"的唯一判据。

它同时被 Graph 的分流（``route_after_parse``）与对外响应（``workflow_status``）读取，
所以它的边界必须被钉死：两边对"失败"的理解一旦不一致，
就会出现"图停下来了但响应说 completed"这类矛盾。
"""

from __future__ import annotations

import pytest

from app.graph.state import has_usable_document
from app.schemas.document import ParseResult


def _result(status: str) -> ParseResult:
    return ParseResult(status=status, parser="DocxParser", source_file_type="DOCX")


@pytest.mark.parametrize("status", ["PARSED", "EMPTY"])
def test_parsed_and_empty_are_usable(status: str) -> None:
    """EMPTY 是可用的 —— 空文档是**数据问题**（合同本身没内容），不是我们没读出来。"""
    assert has_usable_document({"parse_result": _result(status)}) is True


def test_failed_is_not_usable() -> None:
    assert has_usable_document({"parse_result": _result("FAILED")}) is False


def test_missing_parse_result_is_not_usable() -> None:
    """fail-closed：解析根本没跑过，就不能当成拿到了文档。"""
    assert has_usable_document({}) is False
    assert has_usable_document({"parse_result": None}) is False


def test_other_state_fields_do_not_affect_the_judgement() -> None:
    """判据只看解析结果 —— 上传成功与否不影响"文档能不能用"。"""
    state = {"file_valid": True, "contract_id": 11, "parse_result": _result("FAILED")}

    assert has_usable_document(state) is False
