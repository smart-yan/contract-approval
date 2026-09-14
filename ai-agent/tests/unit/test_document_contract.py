"""``ParseResult`` / ``Paragraph`` 的位置定位契约（P6-2）。

这个文件测的**不是 DOCX 解析**，而是"解析产出的结构能不能被下游可靠地定位"。
它是 P7 条款识别、P9 风险定位、P11 原文高亮的共同前提：

* 段落序号连续且稳定 ⇒ 风险项能指回确切的一段
* ``text`` 里的每个 ``\\n`` 都是段落边界 ⇒ 拿到一段文本就能知道它在第几段
* 段落文本里不含换行 ⇒ 上面这条才成立（这正是本轮修掉的歧义）

DOCX 格式细节（表格、中文、损坏文件）由 ``test_docx_parser.py`` 负责。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.parsers import parse_document_file
from app.parsers.docx_parser import normalize_text
from tests.factories import save_docx, soft_break_paragraph

CHINESE = (
    "软件采购合同",
    "甲方：某某科技有限公司",
    "第一条 乙方应于收到货物后 30 日内支付全部货款。",
)


def _parse(path: Path):
    return parse_document_file(path, file_type="DOCX")


# --------------------------------------------------------------------------- #
# 不变量 1：段落序号连续、等于列表位置 —— 它就是"文档位置"的标识
# --------------------------------------------------------------------------- #
def test_paragraph_index_is_contiguous_and_matches_list_position(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "c.docx", *CHINESE)

    result = _parse(path)

    assert [p.index for p in result.paragraphs] == list(range(len(CHINESE)))
    # 序号即位置 ⇒ 断言可以互推，不会有"序号对但顺序乱"的中间状态
    assert [result.paragraphs[i].text for i in range(len(CHINESE))] == list(CHINESE)


def test_index_survives_empty_paragraphs(tmp_path: Path) -> None:
    """空段落必须保留 —— 丢掉它们会让后面的段落序号整体前移，与原文对不上。"""
    path = save_docx(tmp_path / "gaps.docx", "第一段", "", "   ", "第四段")

    result = _parse(path)

    assert len(result.paragraphs) == 4, "空段落不能被吞掉"
    assert [p.index for p in result.paragraphs] == [0, 1, 2, 3]
    assert result.paragraphs[1].text == ""
    assert result.paragraphs[2].text == ""
    assert result.paragraphs[3].text == "第四段"


# --------------------------------------------------------------------------- #
# 不变量 2：text 与 paragraphs 互为可还原（本轮的核心修复）
# --------------------------------------------------------------------------- #
def test_text_equals_newline_join_of_paragraphs(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "j.docx", *CHINESE, "第四段")

    result = _parse(path)

    assert result.text == "\n".join(p.text for p in result.paragraphs)


def test_paragraph_boundaries_are_recoverable_from_text(tmp_path: Path) -> None:
    """位置契约的核心：只看 ``text`` 也能还原出每一段。

    如果段落文本自带 ``\\n``，这个还原就会多切出几段 —— 风险项与高亮随之错位。
    """
    path = save_docx(
        tmp_path / "recover.docx",
        "第一段",
        soft_break_paragraph("这一段的作者插了", "一个软换行"),
        "第三段",
    )

    result = _parse(path)

    recovered = result.text.split("\n")
    assert recovered == [p.text for p in result.paragraphs]
    assert len(recovered) == 3, "软换行不能被当成段落边界"


@pytest.mark.parametrize(
    "block",
    [
        soft_break_paragraph("前", "后"),
        "含制表符\t的段落",
        "  前后都有空格  ",
        "连续     空格",
        "全角　空格",
    ],
)
def test_no_paragraph_text_contains_a_newline(tmp_path: Path, block) -> None:
    """段落文本一律压成单行 —— 这是 ``text`` 可逆的前提。"""
    path = save_docx(tmp_path / "single.docx", block)

    result = _parse(path)

    assert all("\n" not in p.text for p in result.paragraphs)
    assert all("\r" not in p.text for p in result.paragraphs)


# --------------------------------------------------------------------------- #
# 不变量 3：清洗规则明确且可预期
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  前后空白  ", "前后空白"),
        ("连续     空格", "连续 空格"),
        ("制表\t符", "制表 符"),
        ("软\n换行", "软 换行"),
        ("\n\r\t ", ""),
        ("", ""),
        ("   ", ""),
        ("中文　全角空格", "中文 全角空格"),
    ],
)
def test_normalize_text_rule_is_explicit(raw: str, expected: str) -> None:
    """清洗规则写死在这里：连续空白折叠为一个普通空格，再去掉首尾。"""
    assert normalize_text(raw) == expected


def test_soft_break_becomes_a_space_not_a_boundary(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "br.docx", soft_break_paragraph("甲方：", "某某科技有限公司"))

    result = _parse(path)

    assert len(result.paragraphs) == 1, "软换行不是段落边界"
    assert result.paragraphs[0].text == "甲方： 某某科技有限公司"


# --------------------------------------------------------------------------- #
# 表格：\t 只作为列边界出现
# --------------------------------------------------------------------------- #
def test_plain_paragraphs_never_contain_a_tab(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "notab.docx", "普通段落", "含制表\t符的段落")

    result = _parse(path)

    assert all("\t" not in p.text for p in result.paragraphs), "\t 被清洗成空格了，不该出现"


def test_tabs_appear_only_as_table_column_boundaries(tmp_path: Path) -> None:
    path = save_docx(
        tmp_path / "table.docx",
        "付款计划：",
        [["阶段", "比例", "时间"], ["到货款", "100%", "30 日"]],
    )

    result = _parse(path)

    row = result.paragraphs[1]
    assert row.text == "阶段\t比例\t时间"
    assert row.text.count("\t") == 2, "三列 ⇒ 恰好两个列边界"


def test_table_cell_content_is_normalized_too(tmp_path: Path) -> None:
    """单元格里也可能有软换行/制表符 —— 不清洗就会冒充成列边界。"""
    path = save_docx(tmp_path / "cell.docx", [["含\n换行的单元格", "B"]])

    result = _parse(path)

    assert result.paragraphs[0].text == "含 换行的单元格\tB"
    assert result.paragraphs[0].text.count("\t") == 1


# --------------------------------------------------------------------------- #
# 顺序稳定性：段落与表格按文档顺序交错，重复解析结果一致
# --------------------------------------------------------------------------- #
def test_document_order_is_preserved_across_blocks(tmp_path: Path) -> None:
    path = save_docx(
        tmp_path / "order.docx",
        "段一",
        [["表头"]],
        "段二",
        [["表尾"]],
        "段三",
    )

    result = _parse(path)

    assert [p.text for p in result.paragraphs] == ["段一", "表头", "段二", "表尾", "段三"]


def test_parsing_is_deterministic(tmp_path: Path) -> None:
    """同一份文件解析两次必须完全一致 —— 否则存下来的段落序号第二天就失效了。"""
    path = save_docx(tmp_path / "det.docx", *CHINESE, [["A", "B"]], soft_break_paragraph("x", "y"))

    first = _parse(path)
    second = _parse(path)

    assert first.text == second.text
    assert [p.model_dump() for p in first.paragraphs] == [p.model_dump() for p in second.paragraphs]
    assert first.parser == second.parser


# --------------------------------------------------------------------------- #
# 中文与失败结果的契约
# --------------------------------------------------------------------------- #
def test_chinese_text_is_preserved_verbatim(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "cn.docx", "金额：人民币壹拾万元整（¥100,000.00）")

    result = _parse(path)

    assert result.paragraphs[0].text == "金额：人民币壹拾万元整（¥100,000.00）"


def test_failed_result_has_empty_text_and_paragraphs(tmp_path: Path) -> None:
    """失败时两个字段都空 —— 不变量 2 在失败路径上同样成立（空 join 空）。"""
    path = tmp_path / "broken.docx"
    path.write_bytes(b"PK\x03\x04 not really a zip")

    result = _parse(path)

    assert result.status == "FAILED"
    assert result.text == ""
    assert result.paragraphs == []
    assert result.text == "\n".join(p.text for p in result.paragraphs)
