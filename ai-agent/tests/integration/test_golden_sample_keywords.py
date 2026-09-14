"""黄金样例合同的关键词抽取验收。

``samples/采购合同-风险版.docx`` 是 P7/P8/P9/P11 的长期验收基准。
这里把主题词命中逐条钉死 —— 词表或扫描规则一改，这份测试立刻失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.graph.nodes.extract_keywords import extract_keywords as extract_keywords_node
from app.parsers import parse_document_file
from app.understanding.keywords import extract_keywords

SAMPLE = Path(__file__).resolve().parents[3] / "samples" / "采购合同-风险版.docx"

#: 黄金样例的命中总数与不同词数。
#: ⚠️ **刻意钉死**：词表增删一个词、或扫描规则改一点，这里立刻失败 ——
#: 这是有意的，主题索引的变化必须被显式确认。
EXPECTED_TOTAL_HITS = 38
EXPECTED_DISTINCT_TERMS = 19

#: 关键主题词的命中位置（这些是 P8/P11 最可能用到的信号）
EXPECTED_TERM_PARAGRAPHS = {
    "知识产权": [22, 23],
    "保密": [25, 26, 27],
    "违约": [28, 29],
    "仲裁": [32, 33],
    "争议": [31, 32],
    "预付款": [18],
    "验收": [11, 13, 14],
}


@pytest.fixture(scope="module")
def parsed():
    assert SAMPLE.is_file(), f"黄金样例缺失：{SAMPLE}（它是被提交进仓库的验收基准）"
    return parse_document_file(SAMPLE, file_type="DOCX")


@pytest.fixture(scope="module")
def hits(parsed):
    return extract_keywords(parsed)


def _paragraphs_of(hits_, term: str) -> list[int]:
    return sorted(h.paragraph_index for h in hits_ if h.term == term)


# --------------------------------------------------------------------------- #
# 命中规模
# --------------------------------------------------------------------------- #
def test_hit_and_term_counts(hits) -> None:
    assert len(hits) == EXPECTED_TOTAL_HITS
    assert len({h.term for h in hits}) == EXPECTED_DISTINCT_TERMS


@pytest.mark.parametrize(("term", "expected"), sorted(EXPECTED_TERM_PARAGRAPHS.items()))
def test_term_hits_expected_paragraphs(hits, term: str, expected: list[int]) -> None:
    assert _paragraphs_of(hits, term) == expected


# --------------------------------------------------------------------------- #
# 不变量
# --------------------------------------------------------------------------- #
def test_no_duplicate_pairs(hits) -> None:
    pairs = [(h.term, h.paragraph_index) for h in hits]
    assert len(pairs) == len(set(pairs))


def test_every_hit_term_really_appears_in_that_paragraph(hits, parsed) -> None:
    """每条命中都必须能在它声明的那一段里找到那个词 —— 这是结果可核对的前提。"""
    for hit in hits:
        assert hit.term in parsed.paragraphs[hit.paragraph_index].text, (
            f"{hit.term} 不在第 {hit.paragraph_index} 段里"
        )


def test_output_is_ordered_by_paragraph_then_lexicon(hits) -> None:
    from app.understanding.keywords import KEYWORD_LEXICON

    order = {term: i for i, term in enumerate(KEYWORD_LEXICON)}
    keys = [(h.paragraph_index, order[h.term]) for h in hits]

    assert keys == sorted(keys)


def test_extraction_is_deterministic(parsed, hits) -> None:
    again = extract_keywords(parsed)

    assert [h.model_dump() for h in again] == [h.model_dump() for h in hits]


# --------------------------------------------------------------------------- #
# 表格行
# --------------------------------------------------------------------------- #
def test_payment_table_rows_are_scanned(hits, parsed) -> None:
    """付款计划表（17..20 段）是表格行 —— 只扫正文会漏掉整张付款安排。"""
    table_rows = [p.index for p in parsed.paragraphs if p.block_type == "TABLE_ROW"]
    assert table_rows == [17, 18, 19, 20]

    hit_paragraphs = {h.paragraph_index for h in hits}
    assert set(table_rows) <= hit_paragraphs, "每一行付款计划都应当有主题词命中"


def test_overlapping_terms_in_one_table_row(hits) -> None:
    """第 18 段「1. 预付款 30% 合同生效后…支付」同时命中三个词 —— 刻意保留的行为。"""
    terms = sorted(h.term for h in hits if h.paragraph_index == 18)

    assert terms == ["付款", "支付", "预付款"]


# --------------------------------------------------------------------------- #
# 与风险无关
# --------------------------------------------------------------------------- #
def test_keywords_carry_no_risk_signal(hits) -> None:
    """P7-3 不判断风险；命中列表里不存在任何风险含义的字段。"""
    for hit in hits:
        assert set(hit.model_dump()) == {"term", "paragraph_index"}


# --------------------------------------------------------------------------- #
# 端到端：节点写进 State
# --------------------------------------------------------------------------- #
def test_node_writes_the_same_hits_into_state(parsed, hits) -> None:
    updates = extract_keywords_node({"parse_result": parsed, "file_id": 1})

    assert updates["keywords"] == hits
