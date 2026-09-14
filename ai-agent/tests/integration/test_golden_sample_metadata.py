"""黄金样例合同的元数据抽取验收。

``samples/采购合同-风险版.docx`` 是 P7/P8/P9/P11 的长期验收基准。
这里把它的元数据逐字段钉死 —— 抽取规则一旦漂移，前端展示的"合同基本信息"
就会悄悄变样。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.constants import ExtractMethod
from app.graph.nodes.extract_metadata import extract_metadata as extract_metadata_node
from app.parsers import parse_document_file
from app.understanding.metadata import extract_metadata

SAMPLE = Path(__file__).resolve().parents[3] / "samples" / "采购合同-风险版.docx"

#: (field_key, field_value, value_type, paragraph_index)
EXPECTED = [
    ("our_party_name", "启明数智科技（上海）有限公司", "TEXT", 3),
    ("counterparty_name", "远航软件技术有限公司", "TEXT", 5),
    ("credit_code", "91310115MA1K3QXW2P", "CODE", 4),
    ("contract_amount", "1200000.00", "AMOUNT", 10),
    ("currency", "CNY", "TEXT", 10),
    ("sign_date", "2026-09-14", "DATE", 40),
    ("expire_date", "2029-09-13", "DATE", 35),
]


@pytest.fixture(scope="module")
def parsed():
    assert SAMPLE.is_file(), f"黄金样例缺失：{SAMPLE}（它是被提交进仓库的验收基准）"
    return parse_document_file(SAMPLE, file_type="DOCX")


@pytest.fixture(scope="module")
def items(parsed):
    return extract_metadata(parsed)


def _by_key(items_, key: str):
    return next((i for i in items_ if i.field_key == key), None)


# --------------------------------------------------------------------------- #
# 逐字段钉死
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("key", "value", "value_type", "index"), EXPECTED)
def test_field_matches_expectation(items, key: str, value: str, value_type: str, index: int) -> None:
    item = _by_key(items, key)

    assert item is not None, f"{key} 没抽到"
    assert item.field_value == value
    assert item.value_type == value_type
    assert item.paragraph_index == index


def test_payment_terms_is_the_payment_table(items) -> None:
    item = _by_key(items, "payment_terms")

    assert item is not None
    rows = item.field_value.split("\n")
    assert rows[0] == "付款阶段\t比例\t付款条件"
    assert "1. 预付款\t30%\t合同生效后五（5）个工作日内支付" in rows
    assert len(rows) == 4, "整个付款计划表（表头 + 3 行）"


# --------------------------------------------------------------------------- #
# 契约与不变量
# --------------------------------------------------------------------------- #
def test_effective_date_is_absent(items) -> None:
    """样例里没有显式生效日（只写「自双方盖章之日起生效」）⇒ 该字段不产出。

    ⚠️ 同一段（第 35 段）里还有「有效期至 2029 年 9 月 13 日」。
    如果实现退化成"在这一段里找日期"，生效日就会被**错填成到期日** ——
    这条断言正是那道防线。
    """
    assert _by_key(items, "effective_date") is None
    assert _by_key(items, "expire_date").field_value == "2029-09-13"


def test_extracted_field_set_is_exactly_the_available_fields(items) -> None:
    """黄金样例能抽到的字段**正好**是这些（顺序 = 字段目录顺序）。

    ``payment_terms`` / ``prepay_ratio`` 都来自付款计划表：
    前者是整块原文（人工核对用），后者是表里的比例数值（规则比较用）。
    """
    assert [i.field_key for i in items] == [key for key, *_ in EXPECTED] + ["payment_terms", "prepay_ratio"]


def test_prepay_ratio_is_read_from_the_payment_table(items) -> None:
    """预付款比例：**结构化事实**，来自付款计划表里的一行。"""
    item = _by_key(items, "prepay_ratio")

    assert item is not None, "黄金样例的付款表里有「1. 预付款 | 30%」"
    assert item.field_value == "0.3", "30% 规范化为比例 0.3（纯数字串）"
    assert item.value_type == "RATIO"
    assert item.paragraph_index == 18
    assert item.quote == "1. 预付款\t30%\t合同生效后五（5）个工作日内支付"


def test_every_item_has_a_quote_pointing_at_real_text(items, parsed) -> None:
    """每条都必须能指回原文。

    绝大多数字段的 quote 是单段文本；块级字段（付款条件）的 quote 跨连续多段，
    此时它的**第一行**必须落在 ``paragraph_index`` 指的那一段里。
    """
    for item in items:
        assert item.quote, f"{item.field_key} 缺 quote"
        first_line = item.quote.split("\n")[0]
        source = parsed.paragraphs[item.paragraph_index].text
        assert first_line in source or item.field_value in source, f"{item.field_key} 的 quote 对不上原文"


def test_block_level_quote_lines_map_to_consecutive_paragraphs(items, parsed) -> None:
    """块级 quote 的每一行都要能对上一个真实段落 —— 否则前端按行定位会错位。"""
    item = _by_key(items, "payment_terms")

    for offset, line in enumerate(item.quote.split("\n")):
        index = item.paragraph_index + offset
        assert parsed.paragraphs[index].text == line, f"第 {offset} 行对不上段落 {index}"


def test_party_names_match_the_document_not_the_signature_block(items) -> None:
    """主体声明在第 3/5 段；签署栏（第 38/41 段）不是来源。"""
    assert _by_key(items, "our_party_name").paragraph_index == 3
    assert _by_key(items, "counterparty_name").paragraph_index == 5


def test_extract_method_is_regex_for_every_item(items) -> None:
    assert {i.extract_method for i in items} == {ExtractMethod.REGEX.value}


def test_extraction_is_deterministic(parsed, items) -> None:
    again = extract_metadata(parsed)

    assert [i.model_dump() for i in again] == [i.model_dump() for i in items]


# --------------------------------------------------------------------------- #
# 端到端：节点写进 State
# --------------------------------------------------------------------------- #
def test_node_writes_the_same_items_into_state(parsed, items) -> None:
    updates = extract_metadata_node({"parse_result": parsed, "file_id": 1})

    assert updates["metadata"] == items
