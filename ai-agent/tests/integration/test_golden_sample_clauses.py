"""黄金样例合同的条款切分验收。

``samples/采购合同-风险版.docx`` 是 P7/P8/P9/P11 的**长期验收基准**。
这里把它从"一份文档"跑成"9 个条款"，并逐条钉死区间 ——
这是后续所有阶段共同的起点，一旦切分漂移，下游全部跟着错位。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.constants import ClauseType, ExtractMethod
from app.graph.nodes.identify_clauses import identify_clauses as identify_clauses_node
from app.parsers import parse_document_file
from app.understanding.clauses import identify_clauses as build_clauses

SAMPLE = Path(__file__).resolve().parents[3] / "samples" / "采购合同-风险版.docx"

#: 逐条钉死的期望：(clause_no, start, end)
EXPECTED = [
    (None, 0, 7),  # 前言：标题 / 编号 / 甲乙方 / 信用代码 / 前言段
    ("第一条", 8, 10),
    ("第二条", 11, 14),
    ("第三条", 15, 21),  # 含付款计划表（4 行）
    ("第四条", 22, 24),
    ("第五条", 25, 27),
    ("第六条", 28, 30),
    ("第七条", 31, 33),
    ("第八条", 34, 43),  # 含签署栏
]


@pytest.fixture(scope="module")
def parsed():
    assert SAMPLE.is_file(), f"黄金样例缺失：{SAMPLE}（它是被提交进仓库的验收基准）"
    return parse_document_file(SAMPLE, file_type="DOCX")


@pytest.fixture(scope="module")
def clauses(parsed):
    return build_clauses(parsed)


# --------------------------------------------------------------------------- #
# 切分结果
# --------------------------------------------------------------------------- #
def test_sample_parses(parsed) -> None:
    assert parsed.status == "PARSED"
    assert len(parsed.paragraphs) == 44


def test_exactly_nine_clauses(clauses) -> None:
    assert len(clauses) == len(EXPECTED)


@pytest.mark.parametrize(("clause_no", "start", "end"), EXPECTED)
def test_clause_range_matches_expectation(clauses, clause_no: str | None, start: int, end: int) -> None:
    expected_index = [e[0] for e in EXPECTED].index(clause_no)
    clause = clauses[expected_index]

    assert clause.clause_index == expected_index
    assert clause.clause_no == clause_no
    assert (clause.start_paragraph_index, clause.end_paragraph_index) == (start, end)


@pytest.mark.parametrize(
    ("clause_no", "expected_title"),
    [
        ("第一条", "合同标的与金额"),
        ("第二条", "交付与验收"),
        ("第三条", "付款方式"),
        ("第四条", "知识产权"),
        ("第五条", "保密"),
        ("第六条", "违约责任"),
        ("第七条", "争议解决"),
        ("第八条", "其他"),
    ],
)
def test_clause_title_comes_from_the_heading(clauses, clause_no: str, expected_title: str) -> None:
    """标题只从编号段派生 —— 它是前端条款大纲的直接来源。"""
    clause = next(c for c in clauses if c.clause_no == clause_no)

    assert clause.title == expected_title
    assert clause.text.split("\n")[0].endswith(expected_title), "标题确实来自编号段那一行"


def test_preamble_has_no_title(clauses) -> None:
    assert clauses[0].clause_no is None
    assert clauses[0].title is None


def test_table_rows_do_not_split_the_payment_clause(clauses) -> None:
    """表格里的「1. 预付款 / 2. 到货款 / 3. 质保金」绝不能被当成条款起点。

    第三条覆盖 15..21（含 4 行表格），而不是被切成 4 个条款。
    """
    payment = clauses[3]

    assert payment.clause_no == "第三条"
    assert (payment.start_paragraph_index, payment.end_paragraph_index) == (15, 21)
    assert "付款阶段\t比例\t付款条件" in payment.text
    assert "1. 预付款\t30%" in payment.text, "表格行原样保留在条款文本里"


def test_sub_numbering_does_not_split_clauses(clauses) -> None:
    """``1.1`` / ``1.2`` 是款，不是条款 —— 第一条是 8..10 而不是 8..8。"""
    first = clauses[1]

    assert first.clause_no == "第一条"
    assert (first.start_paragraph_index, first.end_paragraph_index) == (8, 10)
    assert "1.1 " in first.text
    assert "1.2 " in first.text


# --------------------------------------------------------------------------- #
# 不变量
# --------------------------------------------------------------------------- #
def test_clause_ranges_form_a_complete_partition(clauses, parsed) -> None:
    covered: list[int] = []
    for clause in clauses:
        covered.extend(range(clause.start_paragraph_index, clause.end_paragraph_index + 1))

    assert covered == list(range(len(parsed.paragraphs)))


def test_clause_index_is_contiguous(clauses) -> None:
    assert [c.clause_index for c in clauses] == list(range(len(EXPECTED)))


def test_every_clause_text_is_derived_from_paragraphs(clauses, parsed) -> None:
    for clause in clauses:
        start, end = clause.start_paragraph_index, clause.end_paragraph_index
        assert clause.text == "\n".join(p.text for p in parsed.paragraphs[start : end + 1])
        assert len(clause.text.split("\n")) == end - start + 1


def test_extract_method_is_rule_for_all_clauses(clauses) -> None:
    assert {c.extract_method for c in clauses} == {ExtractMethod.RULE.value}


# --------------------------------------------------------------------------- #
# 类型：P8 预置规则命中与否的直接前提
# --------------------------------------------------------------------------- #
def test_ip_clause_is_classified_as_ip(clauses) -> None:
    """``IP_OWNER_SUPPLIER_001`` 规则限定 ``target_clause_types=["IP"]`` ——
    这条判错，黄金链路要求的「知识产权归属供应商」高风险卡片就不会出现。
    """
    ip = clauses[4]

    assert ip.clause_no == "第四条"
    assert ip.clause_type == ClauseType.IP.value
    assert "知识产权归乙方所有" in ip.text, "风险关键词确实在这条里"


def test_liability_clause_is_classified_as_liability(clauses) -> None:
    liability = clauses[6]

    assert liability.clause_no == "第六条"
    assert liability.clause_type == ClauseType.LIABILITY.value
    assert "赔偿甲方的全部损失并承担一切责任" in liability.text


def test_other_clause_types_on_the_sample(clauses) -> None:
    """把全部 9 条的判定结果钉死 —— 分类表一旦被改动，这里立刻暴露。"""
    assert [c.clause_type for c in clauses] == [
        ClauseType.OTHER.value,  # 前言
        ClauseType.SUBJECT.value,  # 第一条 合同标的与金额
        ClauseType.ACCEPTANCE.value,  # 第二条 交付与验收
        ClauseType.AMOUNT_PAYMENT.value,  # 第三条 付款方式
        ClauseType.IP.value,  # 第四条 知识产权
        ClauseType.CONFIDENTIAL.value,  # 第五条 保密
        ClauseType.LIABILITY.value,  # 第六条 违约责任
        ClauseType.DISPUTE.value,  # 第七条 争议解决
        ClauseType.OTHER.value,  # 第八条 其他
    ]


# --------------------------------------------------------------------------- #
# 端到端：节点写进 State
# --------------------------------------------------------------------------- #
def test_node_writes_the_same_clauses_into_state(parsed, clauses) -> None:
    updates = identify_clauses_node({"parse_result": parsed, "file_id": 1})

    assert updates["clauses"] == clauses
