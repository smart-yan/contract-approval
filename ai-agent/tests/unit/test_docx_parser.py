"""``DocxParser`` 与解析入口的单元测试。

覆盖用户点名的 5 类场景（正常 / 空 / 只有空白段落 / 文件不存在 / 不支持的扩展名），
外加中文与表格 —— 后两者是"解析出来的东西够不够 P7 用"的直接检验。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AgentErrorCode
from app.parsers import PARSERS, parse_document_file, resolve_parser
from app.parsers.base import DocumentParseError
from app.parsers.docx_parser import DocxParser
from tests.factories import save_docx, soft_break_paragraph

CHINESE_PARAGRAPHS = (
    "第一条 本合同由甲方与乙方于二〇二六年九月十四日签订。",
    "第二条 乙方应于收到货物后 30 日内支付全部货款，金额为人民币壹拾万元整（¥100,000.00）。",
    "第三条 争议提交上海仲裁委员会仲裁。",
)


# --------------------------------------------------------------------------- #
# 注册与路由
# --------------------------------------------------------------------------- #
def test_docx_parser_is_registered_for_docx() -> None:
    assert any(isinstance(parser, DocxParser) for parser in PARSERS)

    resolved = resolve_parser("DOCX")
    assert resolved is not None
    assert resolved.name == "DocxParser"
    assert resolve_parser("docx") is resolved, "文件类型码大小写不应影响匹配"


@pytest.mark.parametrize("file_type", ["PDF", "JPEG", "PNG", "TIF", "BMP", "UNKNOWN", None, ""])
def test_unsupported_file_types_resolve_to_none(file_type: str | None) -> None:
    """P6-1 只支持 DOCX —— 其余一律解析不了，但要**给出明确原因**而不是崩掉。"""
    assert resolve_parser(file_type) is None

    result = parse_document_file(Path("whatever.docx"), file_type=file_type)

    assert result.status == "FAILED"
    assert result.error_code == AgentErrorCode.PARSE_UNSUPPORTED_TYPE.value
    assert result.text == ""
    assert result.paragraphs == []


# --------------------------------------------------------------------------- #
# 1. 正常 DOCX
# --------------------------------------------------------------------------- #
def test_parses_multi_paragraph_document(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "contract.docx", *CHINESE_PARAGRAPHS)

    result = parse_document_file(path, file_type="DOCX")

    assert result.status == "PARSED"
    assert result.parser == "DocxParser"
    assert result.source_file_type == "DOCX"
    assert result.error_code is None

    # 段落结构：index 连续、顺序与写入一致
    assert [p.index for p in result.paragraphs] == [0, 1, 2]
    assert [p.text for p in result.paragraphs] == list(CHINESE_PARAGRAPHS)

    # 完整纯文本 = 段落按顺序拼接
    assert result.text == "\n".join(CHINESE_PARAGRAPHS)


def test_chinese_text_survives_round_trip(tmp_path: Path) -> None:
    """中文不得出现乱码、丢字或转义 —— 这是合同解析的底线。"""
    path = save_docx(tmp_path / "cn.docx", "甲方：某某科技有限公司", "乙方：另一家（上海）有限公司")

    result = parse_document_file(path, file_type="DOCX")

    assert "某某科技有限公司" in result.text
    assert "另一家（上海）有限公司" in result.text, "中文全角括号也必须原样保留"


def test_table_rows_are_kept_in_document_order(tmp_path: Path) -> None:
    """表格是正文的一部分（金额、付款计划常在里面），绝不能丢。"""
    path = save_docx(
        tmp_path / "with-table.docx",
        "付款计划如下：",
        [["阶段", "比例", "时间"], ["到货款", "100%", "30 日"]],
        "以上为全部付款安排。",
    )

    result = parse_document_file(path, file_type="DOCX")

    assert result.status == "PARSED"
    assert len(result.paragraphs) == 4  # 1 段 + 2 行表格 + 1 段
    assert result.paragraphs[1].text == "阶段\t比例\t时间"
    assert result.paragraphs[2].text == "到货款\t100%\t30 日"
    # 顺序必须保持：表格夹在中间，不能被挪到末尾
    assert result.paragraphs[0].text == "付款计划如下："
    assert result.paragraphs[3].text == "以上为全部付款安排。"


# --------------------------------------------------------------------------- #
# 2 / 3. 空文档 与 只有空白段落
# --------------------------------------------------------------------------- #
def test_empty_document_is_parsed_but_marked_empty(tmp_path: Path) -> None:
    """空文档是**数据问题**，不是解析失败 —— 两者必须分开。"""
    path = save_docx(tmp_path / "empty.docx")

    result = parse_document_file(path, file_type="DOCX")

    assert result.status == "EMPTY"
    assert result.text == ""
    assert result.paragraphs == []
    assert result.error_code is None, "空文档不是错误"


def test_whitespace_only_document_is_empty_not_parsed(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "blank.docx", "   ", "\t", "")

    result = parse_document_file(path, file_type="DOCX")

    assert result.status == "EMPTY"
    assert result.text.strip() == ""
    assert result.error_code is None


# --------------------------------------------------------------------------- #
# 4. 文件不存在 / 不是合法 DOCX
# --------------------------------------------------------------------------- #
def test_missing_file_raises_inside_parser(tmp_path: Path) -> None:
    """Parser 内部用异常表达"我做不到" —— 这是它对单独测试的接口。"""
    parser = DocxParser()

    with pytest.raises(DocumentParseError) as exc_info:
        parser.parse(tmp_path / "not-there.docx")

    assert exc_info.value.code == AgentErrorCode.PARSE_FAILED


def test_missing_file_becomes_failed_result_through_the_entry_point(tmp_path: Path) -> None:
    """但经过统一入口后，异常被收敛成结果 —— Node 不需要 try/except。"""
    result = parse_document_file(tmp_path / "not-there.docx", file_type="DOCX")

    assert result.status == "FAILED"
    assert result.parser == "DocxParser"
    assert result.error_code == AgentErrorCode.PARSE_FAILED.value
    assert "不存在" in (result.error_message or "")


def test_corrupted_docx_becomes_failed_result(tmp_path: Path) -> None:
    """扩展名是 .docx、内容不是 ZIP 的文件（重命名骗过校验的那种）。"""
    path = tmp_path / "broken.docx"
    path.write_bytes(b"this is definitely not a zip archive")

    result = parse_document_file(path, file_type="DOCX")

    assert result.status == "FAILED"
    assert result.error_code == AgentErrorCode.PARSE_FAILED.value
    assert result.text == ""
    assert result.paragraphs == []


# --------------------------------------------------------------------------- #
# 契约不变量
# --------------------------------------------------------------------------- #
def test_text_is_always_the_join_of_paragraphs(tmp_path: Path) -> None:
    """P7 要靠这个不变量从段落偏移推出正文偏移，破坏了定位就会错位。"""
    path = save_docx(tmp_path / "inv.docx", "甲", "乙", [["表头"]], "丙")

    result = parse_document_file(path, file_type="DOCX")

    assert result.text == "\n".join(p.text for p in result.paragraphs)


# --------------------------------------------------------------------------- #
# block_type：段落 / 表格行由解析器显式标注，不从文本反推
# --------------------------------------------------------------------------- #
def test_plain_paragraph_is_marked_as_paragraph(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "p.docx", "第一条 合同标的", "正文段落")

    result = parse_document_file(path, file_type="DOCX")

    assert [p.block_type for p in result.paragraphs] == ["PARAGRAPH", "PARAGRAPH"]


def test_table_rows_are_marked_as_table_row(tmp_path: Path) -> None:
    path = save_docx(
        tmp_path / "t.docx",
        [["付款阶段", "比例"], ["预付款", "30%"]],
    )

    result = parse_document_file(path, file_type="DOCX")

    assert [p.block_type for p in result.paragraphs] == ["TABLE_ROW", "TABLE_ROW"]


def test_single_column_table_row_is_table_row_even_without_a_tab(tmp_path: Path) -> None:
    """**这条就是不能用 ``\\t`` 反推的原因**：单列表格的行根本没有制表符。

    如果靠"文本里有没有 \\t"判断，它会被当成普通段落 —— 而表格里的
    「1. 预付款」正好会被条款识别误认成条款起点。
    """
    path = save_docx(tmp_path / "single.docx", [["1. 预付款"], ["2. 验收款"]])

    result = parse_document_file(path, file_type="DOCX")

    assert [p.block_type for p in result.paragraphs] == ["TABLE_ROW", "TABLE_ROW"]
    assert "\t" not in result.text, "单列表格确实没有制表符 —— 靠它反推必错"
    assert result.paragraphs[0].text == "1. 预付款"


def test_block_types_keep_document_order(tmp_path: Path) -> None:
    """段落与表格按文档顺序交错，block_type 必须跟着一起交错。"""
    path = save_docx(
        tmp_path / "mixed.docx",
        "段一",
        [["表一", "A"]],
        "段二",
        [["表二", "B"]],
        "段三",
    )

    result = parse_document_file(path, file_type="DOCX")

    assert [p.block_type for p in result.paragraphs] == [
        "PARAGRAPH",
        "TABLE_ROW",
        "PARAGRAPH",
        "TABLE_ROW",
        "PARAGRAPH",
    ]
    assert [p.text for p in result.paragraphs] == ["段一", "表一\tA", "段二", "表二\tB", "段三"]


def test_block_type_does_not_disturb_index_or_text_cleaning(tmp_path: Path) -> None:
    """加了 block_type 之后，编号语义与清洗规则**一个字都没变**。"""
    path = save_docx(
        tmp_path / "stable.docx",
        "  前后有空格  ",
        "",
        [["含\n换行的单元格", "B"]],
        soft_break_paragraph("软", "换行"),
    )

    result = parse_document_file(path, file_type="DOCX")

    assert [p.index for p in result.paragraphs] == [0, 1, 2, 3]
    assert [p.text for p in result.paragraphs] == ["前后有空格", "", "含 换行的单元格\tB", "软 换行"]
