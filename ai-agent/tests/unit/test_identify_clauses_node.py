"""``identify_clauses`` 节点的职责：State → 纯函数 → State。

这里只验证**编解码**与失败语义；切分规则本身由 ``test_clause_identification.py`` 负责。
"""

from __future__ import annotations

from app.graph.nodes.identify_clauses import identify_clauses
from app.schemas.document import ParseResult
from tests.factories import make_parse_result, para, row


def _failed_result() -> ParseResult:
    return ParseResult(
        status="FAILED",
        parser="DocxParser",
        source_file_type="DOCX",
        error_code="PARSE_FAILED",
        error_message="文件损坏",
    )


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_node_writes_clauses_into_state() -> None:
    parse_result = make_parse_result(para("第一条 合同标的"), para("正文"))

    updates = identify_clauses({"parse_result": parse_result, "file_id": 7})

    assert set(updates) == {"clauses"}, "只写 clauses，不污染其它 State 字段"
    assert len(updates["clauses"]) == 1
    assert updates["clauses"][0].clause_no == "第一条"


def test_node_does_not_touch_input_fields() -> None:
    parse_result = make_parse_result(para("第一条 甲"), para("正文"))

    updates = identify_clauses({"parse_result": parse_result, "file_id": 7, "file_type": "DOCX"})

    assert "parse_result" not in updates
    assert "file_id" not in updates
    assert "file_type" not in updates


def test_node_handles_document_without_numbering() -> None:
    updates = identify_clauses({"parse_result": make_parse_result(para("甲"), para("乙"))})

    assert len(updates["clauses"]) == 1


def test_node_covers_table_rows(tmp_path=None) -> None:
    parse_result = make_parse_result(para("第一条 付款"), row("1. 预付款", "30%"))

    updates = identify_clauses({"parse_result": parse_result})

    assert updates["clauses"][0].end_paragraph_index == 1


# --------------------------------------------------------------------------- #
# 失败路径：写空列表，绝不抛异常
# --------------------------------------------------------------------------- #
def test_missing_parse_result_yields_empty_clauses() -> None:
    """没有解析结果就切不出条款 —— 这是可预期的结果，不是错误。"""
    updates = identify_clauses({})

    assert updates == {"clauses": []}


def test_none_parse_result_yields_empty_clauses() -> None:
    updates = identify_clauses({"parse_result": None})

    assert updates == {"clauses": []}


def test_failed_parse_result_yields_empty_clauses() -> None:
    updates = identify_clauses({"parse_result": _failed_result()})

    assert updates == {"clauses": []}


def test_node_never_raises() -> None:
    for state in ({}, {"parse_result": None}, {"parse_result": _failed_result()}):
        assert identify_clauses(state) == {"clauses": []}
