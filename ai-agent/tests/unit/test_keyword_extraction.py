"""关键词抽取纯函数（``understanding.keywords.extract_keywords``）。

只测扫描规则本身；真实合同的验收在 ``tests/integration/test_golden_sample_keywords.py``。
"""

from __future__ import annotations

import pytest

from app.understanding.keywords import KEYWORD_LEXICON, extract_keywords
from tests.factories import make_parse_result, para, row


def _pairs(hits) -> list[tuple[str, int]]:
    return [(h.term, h.paragraph_index) for h in hits]


def _terms_at(hits, index: int) -> list[str]:
    return [h.term for h in hits if h.paragraph_index == index]


# --------------------------------------------------------------------------- #
# 正常命中
# --------------------------------------------------------------------------- #
def test_hits_a_single_term() -> None:
    result = make_parse_result(para("第一条 保密"), para("双方负有保密义务。"))

    hits = extract_keywords(result)

    assert _pairs(hits) == [("保密", 0), ("保密", 1)]


def test_hits_multiple_terms_in_one_paragraph() -> None:
    result = make_parse_result(para("乙方逾期交付的，应支付违约金并赔偿损失。"))

    hits = extract_keywords(result)

    assert set(_terms_at(hits, 0)) == {"交付", "支付", "违约", "赔偿", "损失"}


def test_paragraph_index_points_at_the_source_paragraph() -> None:
    result = make_parse_result(para("普通正文"), para("普通正文"), para("本条款涉及仲裁。"))

    hits = extract_keywords(result)

    assert _pairs(hits) == [("仲裁", 2)]


def test_chinese_text_is_matched_verbatim() -> None:
    result = make_parse_result(para("本项目产生的知识产权归甲方所有。"))

    assert _pairs(extract_keywords(result)) == [("知识产权", 0)]


# --------------------------------------------------------------------------- #
# 同一关键词多段命中
# --------------------------------------------------------------------------- #
def test_same_term_hits_every_paragraph_it_appears_in() -> None:
    result = make_parse_result(para("保密义务"), para("与保密无关"), para("保密期限五年"))

    hits = extract_keywords(result)

    assert _pairs(hits) == [("保密", 0), ("保密", 1), ("保密", 2)]


# --------------------------------------------------------------------------- #
# 去重：同一 (term, paragraph) 只产出一条
# --------------------------------------------------------------------------- #
def test_repeated_term_in_one_paragraph_yields_a_single_hit() -> None:
    """一段里出现三次也只算一次 —— 要的是"这一段谈到了付款"，不是词频。"""
    result = make_parse_result(para("付款、付款、再付款，全部付款完成后。"))

    hits = extract_keywords(result)

    assert _pairs(hits) == [("付款", 0)], "同一段的同一个词不能重复产出"


def test_no_duplicate_pairs_across_the_whole_result() -> None:
    result = make_parse_result(para("保密和保密"), para("保密"), row("保密", "保密"))

    pairs = _pairs(extract_keywords(result))

    assert len(pairs) == len(set(pairs)), "结果里不允许出现重复的 (term, paragraph_index)"


# --------------------------------------------------------------------------- #
# 不命中的情况
# --------------------------------------------------------------------------- #
def test_document_without_any_lexicon_term_yields_nothing() -> None:
    result = make_parse_result(para("这是一份普通的说明文档。"), para("没有主题词。"))

    assert extract_keywords(result) == []


def test_empty_document_yields_nothing() -> None:
    assert extract_keywords(make_parse_result()) == []


def test_lexicon_terms_are_matched_as_substrings() -> None:
    """词表里的词是子串匹配，不要求分词 —— 中文合同场景下这是刻意的最小做法。"""
    result = make_parse_result(para("含税总价（¥1,200,000.00）"))

    assert "税率" not in {h.term for h in extract_keywords(result)}


# --------------------------------------------------------------------------- #
# 表格行也扫描
# --------------------------------------------------------------------------- #
def test_table_rows_are_scanned() -> None:
    """付款计划、交付清单常写在表格里 —— 只扫正文会漏掉一大块。"""
    result = make_parse_result(
        para("付款计划如下："),
        row("1. 预付款", "30%"),
        row("2. 尾款", "70%"),
    )

    hits = extract_keywords(result)

    assert ("预付款", 1) in _pairs(hits)
    assert ("尾款", 2) in _pairs(hits)
    assert _terms_at(hits, 0) == ["付款"]


# --------------------------------------------------------------------------- #
# 词表本身
# --------------------------------------------------------------------------- #
def test_lexicon_is_non_empty_and_has_no_duplicates() -> None:
    assert KEYWORD_LEXICON
    assert len(KEYWORD_LEXICON) == len(set(KEYWORD_LEXICON))


def test_lexicon_has_no_single_character_terms() -> None:
    """单字词（如「价」）会把噪声也扫进来 —— 词表刻意只收多字词。"""
    assert all(len(term) >= 2 for term in KEYWORD_LEXICON)


def test_overlapping_terms_both_hit() -> None:
    """``预付款`` 含 ``付款`` —— 两个词各自独立命中，不做最长匹配抑制。

    对"主题索引"来说，两个词同时出现本身就是信息（这段既谈付款、也谈预付款）。
    这条行为是**刻意固定**的，不是副作用。
    """
    result = make_parse_result(para("预付款比例为 30%。"))

    assert _pairs(extract_keywords(result)) == [("预付款", 0), ("付款", 0)]


# --------------------------------------------------------------------------- #
# 输出顺序与确定性
# --------------------------------------------------------------------------- #
def test_output_is_ordered_by_paragraph_then_lexicon_position() -> None:
    result = make_parse_result(para("仲裁与保密"), para("付款"))

    hits = extract_keywords(result)

    # 词表里「保密」排在「仲裁」之前，所以同段内先出保密
    assert _pairs(hits) == [("保密", 0), ("仲裁", 0), ("付款", 1)], "段落顺序优先，同段内按词表顺序"


def test_extraction_is_deterministic() -> None:
    result = make_parse_result(para("知识产权与保密"), row("付款", "验收"))

    first = extract_keywords(result)
    second = extract_keywords(result)

    assert [h.model_dump() for h in first] == [h.model_dump() for h in second]


# --------------------------------------------------------------------------- #
# 不判断风险
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("forbidden", ["is_risk", "risk_level", "severity", "risk"])
def test_hit_carries_no_risk_semantics(forbidden: str) -> None:
    """P7-3 只回答"出现了什么主题词"，**不做风险判断** —— 那是 P8 的事。"""
    from app.schemas.understanding import KeywordHit

    assert forbidden not in KeywordHit.model_fields
