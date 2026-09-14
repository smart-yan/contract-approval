"""``extract_keywords`` 节点的职责：State → 纯函数 → State。

只验证**编解码**与失败语义；扫描规则本身由 ``test_keyword_extraction.py`` 负责。
"""

from __future__ import annotations

from app.graph.nodes.extract_keywords import extract_keywords
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
def test_node_writes_keywords_into_state() -> None:
    parse_result = make_parse_result(para("保密与知识产权"), para("付款"))

    updates = extract_keywords({"parse_result": parse_result, "file_id": 7})

    assert set(updates) == {"keywords"}, "只写 keywords，不污染其它 State 字段"
    assert [(h.term, h.paragraph_index) for h in updates["keywords"]] == [
        ("保密", 0),
        ("知识产权", 0),
        ("付款", 1),
    ]


def test_node_does_not_touch_input_fields() -> None:
    parse_result = make_parse_result(para("保密"))

    updates = extract_keywords({"parse_result": parse_result, "file_id": 7, "file_type": "DOCX"})

    assert "parse_result" not in updates
    assert "file_id" not in updates
    assert "file_type" not in updates


def test_node_scans_table_rows_too() -> None:
    parse_result = make_parse_result(para("计划如下："), row("预付款", "30%"))

    updates = extract_keywords({"parse_result": parse_result})

    assert ("预付款", 1) in [(h.term, h.paragraph_index) for h in updates["keywords"]]


def test_node_yields_empty_list_when_nothing_matches() -> None:
    updates = extract_keywords({"parse_result": make_parse_result(para("普通正文"))})

    assert updates == {"keywords": []}


# --------------------------------------------------------------------------- #
# 失败路径：写空列表，绝不抛异常
# --------------------------------------------------------------------------- #
def test_missing_parse_result_yields_empty_keywords() -> None:
    assert extract_keywords({}) == {"keywords": []}


def test_none_parse_result_yields_empty_keywords() -> None:
    assert extract_keywords({"parse_result": None}) == {"keywords": []}


def test_failed_parse_result_yields_empty_keywords() -> None:
    assert extract_keywords({"parse_result": _failed_result()}) == {"keywords": []}


def test_node_never_raises() -> None:
    for state in ({}, {"parse_result": None}, {"parse_result": _failed_result()}):
        assert extract_keywords(state) == {"keywords": []}
