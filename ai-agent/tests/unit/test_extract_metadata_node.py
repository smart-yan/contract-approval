"""``extract_metadata`` 节点的职责：State → 纯函数 → State。

只验证**编解码**与失败语义；抽取规则本身由 ``test_metadata_extraction.py`` 负责。
"""

from __future__ import annotations

from app.graph.nodes.extract_metadata import extract_metadata
from app.schemas.document import ParseResult
from tests.factories import make_parse_result, para


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
def test_node_writes_metadata_into_state() -> None:
    parse_result = make_parse_result(
        para("甲方：某某有限公司"),
        para("合同金额为 ¥1,200,000.00"),
    )

    updates = extract_metadata({"parse_result": parse_result, "file_id": 7})

    assert set(updates) == {"metadata"}, "只写 metadata，不污染其它 State 字段"
    keys = [item.field_key for item in updates["metadata"]]
    # 金额所在段落里同时带着币种，因此 currency 也会被抽出
    assert keys == ["our_party_name", "contract_amount", "currency"]


def test_node_does_not_touch_input_fields() -> None:
    parse_result = make_parse_result(para("甲方：某某有限公司"))

    updates = extract_metadata({"parse_result": parse_result, "file_id": 7, "file_type": "DOCX"})

    assert "parse_result" not in updates
    assert "file_id" not in updates
    assert "file_type" not in updates


def test_node_yields_empty_list_when_nothing_is_extractable() -> None:
    updates = extract_metadata({"parse_result": make_parse_result(para("普通正文"))})

    assert updates == {"metadata": []}


# --------------------------------------------------------------------------- #
# 失败路径：写空列表，绝不抛异常
# --------------------------------------------------------------------------- #
def test_missing_parse_result_yields_empty_metadata() -> None:
    assert extract_metadata({}) == {"metadata": []}


def test_none_parse_result_yields_empty_metadata() -> None:
    assert extract_metadata({"parse_result": None}) == {"metadata": []}


def test_failed_parse_result_yields_empty_metadata() -> None:
    assert extract_metadata({"parse_result": _failed_result()}) == {"metadata": []}


def test_node_never_raises() -> None:
    for state in ({}, {"parse_result": None}, {"parse_result": _failed_result()}):
        assert extract_metadata(state) == {"metadata": []}
