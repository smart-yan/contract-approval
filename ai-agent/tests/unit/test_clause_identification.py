"""条款切分纯函数（``understanding.clauses.identify_clauses``）。

这个文件测的是**切分规则本身**，不装配 Graph、不读 DOCX —— 输入直接是段落序列。
用真实合同做的端到端验收在 ``tests/integration/test_golden_sample_clauses.py``。
"""

from __future__ import annotations

import pytest

from app.core.constants import ClauseType, ExtractMethod
from app.understanding.clauses import classify_clause_type, identify_clauses
from tests.factories import make_parse_result, para, row


# --------------------------------------------------------------------------- #
# 辅助断言
# --------------------------------------------------------------------------- #
def _ranges(clauses) -> list[tuple[int, int]]:
    return [(c.start_paragraph_index, c.end_paragraph_index) for c in clauses]


def _assert_partition(clauses, total: int) -> None:
    """核心不变量：条款区间构成 ``[0, total-1]`` 的完整划分（无重叠、无遗漏）。"""
    covered: list[int] = []
    for start, end in _ranges(clauses):
        assert start <= end, "区间必须非空且 start <= end"
        covered.extend(range(start, end + 1))
    assert covered == list(range(total)), f"划分不完整或有重叠：{covered}"


# --------------------------------------------------------------------------- #
# 编号模式：TIAO
# --------------------------------------------------------------------------- #
def test_tiao_splits_into_clauses() -> None:
    result = make_parse_result(
        para("第一条 合同标的"),
        para("正文一"),
        para("第二条 付款方式"),
        para("正文二"),
        para("第三条 争议解决"),
    )

    clauses = identify_clauses(result)

    assert [c.clause_no for c in clauses] == ["第一条", "第二条", "第三条"]
    assert _ranges(clauses) == [(0, 1), (2, 3), (4, 4)]
    _assert_partition(clauses, 5)


@pytest.mark.parametrize(
    ("heading", "expected_no"),
    [
        ("第 1 条 交付", "第 1 条"),
        ("第1条 交付", "第1条"),
        ("第 十二 条 交付", "第 十二 条"),
    ],
)
def test_tiao_supports_spacing_and_arabic_numerals(heading: str, expected_no: str) -> None:
    """编号两侧允许空白；中文数字与阿拉伯数字都支持。

    ``clause_no`` 保留**原文**（含内部空格）—— 它是文档里真实写着的东西，不做归一化。
    """
    result = make_parse_result(para(heading), para("正文"))

    clauses = identify_clauses(result)

    assert len(clauses) == 1
    assert clauses[0].clause_no == expected_no
    assert clauses[0].title == "交付"
    assert clauses[0].clause_type == ClauseType.DELIVERY.value


def test_single_tiao_enables_the_mode() -> None:
    """**TIAO 的门槛是 1** —— 补充协议可能只有一个「第一条」。

    如果沿用其他模式的"至少 2 次"门槛，这条会被当成无编号文档，整篇合成一个条款。
    """
    result = make_parse_result(para("第一条 合同标的"), para("1. 甲方向乙方采购。"))

    clauses = identify_clauses(result)

    assert len(clauses) == 1, "TIAO 命中 1 次就应当启用"
    assert clauses[0].clause_no == "第一条"
    assert "1. 甲方向乙方采购。" in clauses[0].text, "正文里的 1. 不应被当成新条款"


# --------------------------------------------------------------------------- #
# 编号模式：CN_NUM / ARAB 的门槛是 2
# --------------------------------------------------------------------------- #
def test_cn_num_splits_when_used_twice() -> None:
    result = make_parse_result(
        para("一、合同标的"),
        para("正文一"),
        para("二、付款方式"),
        para("正文二"),
    )

    clauses = identify_clauses(result)

    assert [c.clause_no for c in clauses] == ["一、", "二、"]
    _assert_partition(clauses, 4)


def test_arab_splits_when_used_twice() -> None:
    result = make_parse_result(para("1. 合同标的"), para("正文一"), para("2. 付款方式"))

    clauses = identify_clauses(result)

    assert [c.clause_no for c in clauses] == ["1.", "2."]


def test_single_arab_does_not_enable_the_mode() -> None:
    """ARAB 与列表项同形，只出现一次不足以说明它是条款编号体系。"""
    result = make_parse_result(para("1. 只有一个"), para("正文"), para("结尾"))

    clauses = identify_clauses(result)

    assert len(clauses) == 1
    assert clauses[0].clause_no is None


def test_tiao_wins_over_arab() -> None:
    """优先级 TIAO > CN_NUM > ARAB：TIAO 命中时，ARAB 的条目并入条款正文。"""
    result = make_parse_result(
        para("第一条 合同标的"),
        para("1.1 甲方向乙方采购。"),
        para("1.2 金额为壹拾万元。"),
        para("第二条 付款方式"),
        para("2.1 验收后支付。"),
    )

    clauses = identify_clauses(result)

    assert [c.clause_no for c in clauses] == ["第一条", "第二条"]
    assert _ranges(clauses) == [(0, 2), (3, 4)], "1.1/1.2/2.1 必须并入条款，不能各自成条"


def test_tiao_wins_over_cn_num() -> None:
    result = make_parse_result(
        para("第一条 合同标的"),
        para("一、子项一"),
        para("二、子项二"),
        para("第二条 付款方式"),
    )

    clauses = identify_clauses(result)

    assert [c.clause_no for c in clauses] == ["第一条", "第二条"]


# --------------------------------------------------------------------------- #
# 1.1 / 1.2 绝不能成为条款边界
# --------------------------------------------------------------------------- #
def test_sub_numbering_never_starts_a_clause() -> None:
    """``(?!\\d)`` 的全部意义：没有它，这条会切成 3 个条款而不是 1 个。"""
    result = make_parse_result(
        para("1. 合同标的"),
        para("1.1 甲方向乙方采购。"),
        para("1.2 金额为壹拾万元。"),
        para("2. 付款方式"),
    )

    clauses = identify_clauses(result)

    assert [c.clause_no for c in clauses] == ["1.", "2."]
    assert _ranges(clauses) == [(0, 2), (3, 3)]


def test_parenthesised_numbering_is_not_a_boundary() -> None:
    """``(1)`` / ``（一）`` 是层级编号，本轮刻意不识别。"""
    result = make_parse_result(
        para("第一条 合同标的"),
        para("（一）甲方向乙方采购。"),
        para("(2) 金额为壹拾万元。"),
        para("第二条 付款方式"),
    )

    clauses = identify_clauses(result)

    assert [c.clause_no for c in clauses] == ["第一条", "第二条"]


# --------------------------------------------------------------------------- #
# TABLE_ROW 不能开启新条款
# --------------------------------------------------------------------------- #
def test_table_row_never_starts_a_clause() -> None:
    """合同表格里的「1. 预付款」与条款编号同形 —— 只有 block_type 能挡住它。"""
    result = make_parse_result(
        para("第一条 付款方式"),
        para("付款计划如下："),
        row("1. 预付款", "30%"),
        row("2. 到货款", "60%"),
        para("第二条 争议解决"),
    )

    clauses = identify_clauses(result)

    assert [c.clause_no for c in clauses] == ["第一条", "第二条"]
    assert _ranges(clauses) == [(0, 3), (4, 4)], "表格行必须属于第一条，不能各自成条"


def test_table_rows_still_belong_to_a_clause() -> None:
    """表格行不参与**起点**识别，但必须**被覆盖**（划分不变量）。"""
    result = make_parse_result(
        para("第一条 付款方式"),
        row("1. 预付款", "30%"),
        row("2. 到货款", "60%"),
    )

    clauses = identify_clauses(result)

    assert len(clauses) == 1
    assert clauses[0].end_paragraph_index == 2
    _assert_partition(clauses, 3)


def test_table_row_alone_cannot_enable_arab_mode() -> None:
    """模式探测也只统计 PARAGRAPH —— 否则表格会贡献多余的命中次数。"""
    result = make_parse_result(
        row("1. 预付款", "30%"),
        row("2. 到货款", "60%"),
        row("3. 质保金", "10%"),
    )

    clauses = identify_clauses(result)

    assert len(clauses) == 1, "表格行再多也不能启用编号模式"
    assert clauses[0].clause_no is None


# --------------------------------------------------------------------------- #
# 前言 / 无编号 / 空文档
# --------------------------------------------------------------------------- #
def test_preamble_becomes_a_clause_without_clause_no() -> None:
    result = make_parse_result(
        para("软件采购合同"),
        para("甲方：某某公司"),
        para("第一条 合同标的"),
        para("正文"),
    )

    clauses = identify_clauses(result)

    assert len(clauses) == 2
    assert clauses[0].clause_no is None
    assert clauses[0].title is None
    assert clauses[0].clause_type == ClauseType.OTHER.value
    assert _ranges(clauses) == [(0, 1), (2, 3)]


def test_no_preamble_when_document_starts_with_a_clause() -> None:
    """文档从第一个条款开始 ⇒ **不生成空前言条款**（那是噪声）。"""
    result = make_parse_result(para("第一条 合同标的"), para("正文"))

    clauses = identify_clauses(result)

    assert len(clauses) == 1
    assert clauses[0].start_paragraph_index == 0


def test_document_without_any_numbering_becomes_one_clause() -> None:
    """没有任何编号 ⇒ 整篇一个条款，**绝不返回空列表**。"""
    result = make_parse_result(para("甲"), para("乙"), para("丙"))

    clauses = identify_clauses(result)

    assert len(clauses) == 1
    assert clauses[0].clause_no is None
    assert clauses[0].text == "甲\n乙\n丙"


def test_empty_document_returns_empty_list() -> None:
    """唯一返回空列表的情况：一个段落都没有。"""
    assert identify_clauses(make_parse_result()) == []


# --------------------------------------------------------------------------- #
# 空段落
# --------------------------------------------------------------------------- #
def test_empty_paragraphs_are_kept_and_belong_to_the_preceding_clause() -> None:
    result = make_parse_result(
        para("第一条 合同标的"),
        para("正文"),
        para(""),
        para("   "),
        para("第二条 付款方式"),
    )

    clauses = identify_clauses(result)

    assert _ranges(clauses) == [(0, 3), (4, 4)], "空段落不能单独成条，也不能被跳过"
    assert clauses[0].text.split("\n")[2:] == ["", ""]
    _assert_partition(clauses, 5)


# --------------------------------------------------------------------------- #
# 正文 / 表格混合顺序
# --------------------------------------------------------------------------- #
def test_mixed_paragraph_and_table_order_is_preserved() -> None:
    result = make_parse_result(
        para("第一条 交付与验收"),
        para("交付计划如下："),
        row("阶段", "时间"),
        row("部署", "第 1 周"),
        para("第二条 保密"),
        para("正文"),
    )

    clauses = identify_clauses(result)

    assert _ranges(clauses) == [(0, 3), (4, 5)]
    assert clauses[0].text.split("\n") == [
        "第一条 交付与验收",
        "交付计划如下：",
        "阶段\t时间",
        "部署\t第 1 周",
    ]
    _assert_partition(clauses, 6)


# --------------------------------------------------------------------------- #
# clause_index / text / extract_method
# --------------------------------------------------------------------------- #
def test_clause_index_is_contiguous_from_zero() -> None:
    result = make_parse_result(
        para("前言"),
        para("第一条 甲"),
        para("第二条 乙"),
        para("第三条 丙"),
    )

    clauses = identify_clauses(result)

    assert [c.clause_index for c in clauses] == [0, 1, 2, 3]


def test_clause_text_is_derived_from_paragraphs_verbatim() -> None:
    """``Clause.text`` 严格由段落文本派生 —— 不二次清洗、不重新拼装。"""
    result = make_parse_result(
        para("第一条 付款方式"),
        para("含制表符与空格的原文"),
        row("1. 预付款", "30%"),
    )

    clauses = identify_clauses(result)
    paragraphs = result.paragraphs
    start, end = clauses[0].start_paragraph_index, clauses[0].end_paragraph_index

    assert clauses[0].text == "\n".join(p.text for p in paragraphs[start : end + 1])
    assert clauses[0].text.split("\n")[2] == "1. 预付款\t30%", "表格行文本必须原样保留"
    assert len(clauses[0].text.split("\n")) == end - start + 1


def test_extract_method_is_always_rule() -> None:
    result = make_parse_result(para("第一条 甲"), para("正文"), para("第二条 乙"))

    clauses = identify_clauses(result)

    assert {c.extract_method for c in clauses} == {ExtractMethod.RULE.value}


# --------------------------------------------------------------------------- #
# title：只从编号段派生
#
# "是否跨多段"与"编号段里有没有标题"是两个不同维度 —— 标题的有无**只取决于**
# 编号段去掉编号后还剩不剩非空文字。
# --------------------------------------------------------------------------- #
def test_single_paragraph_clause_still_has_a_title() -> None:
    """单段条款一样可以有标题 —— 标题就在编号后面。"""
    result = make_parse_result(para("第一条 合同标的"), para("第二条 付款方式"))

    clauses = identify_clauses(result)

    assert clauses[0].clause_no == "第一条"
    assert clauses[0].title == "合同标的"
    assert clauses[0].text == "第一条 合同标的", "标题不影响条款全文"


def test_multi_paragraph_clause_title_comes_from_the_heading_only() -> None:
    """标题只来自编号段，**不去后续正文段落里找**。"""
    result = make_parse_result(
        para("第一条 合同标的"),
        para("这段正文里有一句看起来很像标题的话"),
        para("第二条 付款方式"),
    )

    clauses = identify_clauses(result)

    assert clauses[0].title == "合同标的"


def test_bare_clause_number_has_no_title() -> None:
    """编号段只写了编号 ⇒ 没有标题可提取。"""
    result = make_parse_result(para("第一条"), para("正文"), para("第二条 付款方式"))

    clauses = identify_clauses(result)

    assert clauses[0].clause_no == "第一条"
    assert clauses[0].title is None
    assert clauses[0].text.split("\n")[0] == "第一条"


def test_title_does_not_depend_on_paragraph_span() -> None:
    """同一个标题，跨 1 段与跨 3 段的结果必须一致 —— 这正是不该用跨段数判断的原因。"""
    single = identify_clauses(make_parse_result(para("第一条 合同标的"), para("第二条 乙")))
    multi = identify_clauses(
        make_parse_result(para("第一条 合同标的"), para("正文一"), para("正文二"), para("第二条 乙"))
    )

    assert single[0].title == multi[0].title == "合同标的"


def test_preamble_and_unnumbered_document_have_no_title() -> None:
    preamble = identify_clauses(make_parse_result(para("软件采购合同"), para("第一条 合同标的")))
    unnumbered = identify_clauses(make_parse_result(para("甲"), para("乙")))

    assert preamble[0].clause_no is None and preamble[0].title is None
    assert unnumbered[0].clause_no is None and unnumbered[0].title is None


# --------------------------------------------------------------------------- #
# ClauseType 分类
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("第四条 知识产权", ClauseType.IP),
        ("第五条 保密", ClauseType.CONFIDENTIAL),
        ("第六条 违约责任", ClauseType.LIABILITY),
        ("第七条 争议解决", ClauseType.DISPUTE),
        ("第三条 付款方式", ClauseType.AMOUNT_PAYMENT),
        ("第二条 交付与验收", ClauseType.ACCEPTANCE),
        ("第一条 合同标的与金额", ClauseType.SUBJECT),
        ("不可抗力条款", ClauseType.FORCE_MAJEURE),
        ("数据安全与个人信息保护", ClauseType.DATA_SECURITY),
        ("第八条 其他", ClauseType.OTHER),
        ("第一条 完全无关的标题", ClauseType.OTHER),
    ],
)
def test_clause_type_classification(heading: str, expected: ClauseType) -> None:
    assert classify_clause_type(heading) is expected


def test_classification_ignores_the_clause_body() -> None:
    """**本轮重点防的实现错误**：分类只看编号段，不重扫条款全文。

    标题是「其他」，正文里却大谈知识产权 —— 如果实现去扫全文，就会被判成 IP。
    """
    result = make_parse_result(
        para("第一条 其他约定"),
        para("本合同涉及知识产权、著作权与专利，并约定保密与违约责任。"),
    )

    clauses = identify_clauses(result)

    assert clauses[0].clause_type == ClauseType.OTHER.value


def test_preamble_is_always_other() -> None:
    result = make_parse_result(para("知识产权归属说明"), para("第一条 其他"))

    clauses = identify_clauses(result)

    assert clauses[0].clause_no is None
    assert clauses[0].clause_type == ClauseType.OTHER.value
