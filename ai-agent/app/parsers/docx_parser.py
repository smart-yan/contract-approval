"""DOCX 解析器（P6-1）。

技术选型：``python-docx``
------------------------
* **为什么需要**：DOCX 是一个 ZIP 容器 + 一整套 OOXML 规范。手写解析要处理命名空间、
  run 合并、表格、编号、软换行等一堆边角，而这些边角正是真实合同里最常见的形态。
* **为什么选它**：它是 Python 生态里 DOCX 读写的事实标准，纯 Python（底层 lxml 是
  预编译 wheel，Windows + 3.12 实测可直接安装），API 稳定且可读。架构文档 §0.2
  第 8 项也早已选定它。
* **是否只用于 Agent**：是。Backend 不解析文档（解析属 Agent，§1.1 职责边界），
  因此这个依赖只加在 ``ai-agent/pyproject.toml``。
* **有没有更简单的方案**：用标准库 ``zipfile`` + ``ElementTree`` 抠 ``w:t`` 也能跑通
  最简单的段落，但会漏掉表格、软换行、超链接文本等，属于"现在省事、以后返工"。

正文的顺序与表格
--------------
``document.paragraphs`` **只给正文段落，完全看不到表格**（已实测）。合同里的金额、
付款计划、交付清单大量存在于表格中，直接漏掉等于丢失正文 —— 这是正确性问题，不是
简化。因此这里按 ``body`` 元素的**文档顺序**遍历，把表格行也收进段落序列：

* ``w:p``   → 一个段落
* ``w:tbl`` → 每一行一条记录，单元格文本用制表符连接

这样 "``text`` 由 ``paragraphs`` 按顺序拼接" 这个不变量成立，P7 的定位也不会错位。

**段落还是表格行 —— 由解析器显式标注，不靠文本反推**
--------------------------------------------------
每条内容都带上 ``block_type``（``PARAGRAPH`` / ``TABLE_ROW``）。这个信息**在遍历的
那一刻就在手上**（当前 child 是 ``w:p`` 还是 ``w:tbl``），丢掉它再去用"文本里有没有
制表符"反推是退而求其次 —— **单列表格的行根本没有制表符**，反推必错。

为什么下游需要它：合同表格里常出现「1. 预付款」这类**看起来像条款编号**的文本。
如果没有结构标记，条款识别会把表格行当成条款起点，切出一堆假条款。

文本清洗规则（P6-2 收口）
----------------------
**段落文本与单元格文本一律压成单行**：所有空白（含 ``<w:br/>`` 换行、``<w:tab/>``
制表符、连续空格）折叠为一个空格，再去掉首尾空白。

为什么必须清洗 —— 这不是"顺手美化"，而是位置契约能否成立的前提：

``ParseResult.text`` 是各段落用 ``\n`` 拼接出来的。而 python-docx 的
``Paragraph.text`` **本身就可能含 ``\n``**（作者插入的软换行会变成换行符，已实测）。
一旦段落内部带 ``\n``，"第几个 ``\n`` 是段落边界"就再也分不出来 ——
``text`` 与 ``paragraphs`` 之间**不可逆**，P9 存下的段落位置、P11 的原文高亮
全部会指错地方。

清洗后有三个可断言的不变量：

1. 段落文本**不含** ``\n``
2. 段落文本**不含** ``\t``（单元格内也清洗过，所以 ``\t`` 只可能是单元格分隔符）
3. 因此 ``text`` 里的每个 ``\n`` 都是一个段落边界 —— **位置可逆**

⚠️ 代价：作者插入的软换行会被抹平成空格。对合同文本分析来说这是可接受的 ——
软换行是排版行为，不是语义边界；而"能定位"是不能让的。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import docx
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph as DocxParagraph

from app.core.errors import AgentErrorCode
from app.parsers.base import DocumentParseError, DocumentParser
from app.schemas.document import Paragraph, ParseResult

#: 段落块类型（与 Backend ``constants.BlockType`` 的取值一致）
_BLOCK_PARAGRAPH = "PARAGRAPH"
_BLOCK_TABLE_ROW = "TABLE_ROW"

#: 表格行内单元格之间的连接符（制表符，与文本抽取工具的惯例一致）
_CELL_SEPARATOR = "\t"

#: 任何连续的空白（空格 / 制表符 / 换行 / 全角空格……）。
#: Python 的 ``\s`` 对 str 默认按 Unicode 匹配，中文全角空格（U+3000）也算 ——
#: 中文合同里它常被当作分隔符，折叠成普通空格符合预期。
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """把一段文本压成**单行**：连续空白折叠为一个空格，并去掉首尾空白。

    规则与理由见模块 docstring —— 它的作用是保证 ``\\n`` 只作为**段落分隔符**出现，
    从而让 ``ParseResult.text`` 与 ``paragraphs`` 互为可还原。
    """
    return _WHITESPACE_RUN.sub(" ", text).strip()


class DocxParser(DocumentParser):
    """把 ``.docx`` 解析成段落序列 + 完整纯文本。"""

    name = "DocxParser"
    supported_file_types = frozenset({"DOCX"})

    def parse(self, path: Path) -> ParseResult:
        if not path.is_file():
            raise DocumentParseError(
                AgentErrorCode.PARSE_FAILED,
                f"待解析文件不存在：{path}",
            )

        try:
            document = docx.Document(str(path))
        except Exception as exc:
            # 实测非 ZIP 内容会抛 docx.opc.exceptions.PackageNotFoundError，
            # 但加密 / 半截文件等的表现不保证一致，因此统一收敛
            raise DocumentParseError(
                AgentErrorCode.PARSE_FAILED,
                f"无法打开 DOCX（{type(exc).__name__}）：不是合法的 DOCX 文件或已损坏",
            ) from exc

        paragraphs = [
            Paragraph(index=i, block_type=block_type, text=text)
            for i, (block_type, text) in enumerate(self._iter_blocks(document))
        ]
        text = "\n".join(p.text for p in paragraphs)

        return ParseResult(
            status="PARSED" if text.strip() else "EMPTY",
            parser=self.name,
            source_file_type="DOCX",
            text=text,
            paragraphs=paragraphs,
        )

    # ------------------------------------------------------------------ #
    def _iter_blocks(self, document: DocxDocument) -> Iterator[tuple[str, str]]:
        """按**文档顺序**产出 ``(block_type, text)``。

        顺序很关键：P7 要靠段落顺序还原"上下文"，乱序会让条款与金额对不上。

        ``block_type`` 在这里是**免费**的 —— 循环变量 ``child`` 自己就说明了
        这一条是段落还是表格。丢掉它再去猜，只会猜错。
        """
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                yield _BLOCK_PARAGRAPH, normalize_text(DocxParagraph(child, document).text)
            elif child.tag == qn("w:tbl"):
                for row in Table(child, document).rows:
                    # 单元格先各自压成单行，再用 \t 拼接 —— 于是 \t 只可能是列边界
                    yield (
                        _BLOCK_TABLE_ROW,
                        _CELL_SEPARATOR.join(normalize_text(cell.text) for cell in row.cells),
                    )


__all__ = ["DocxParser", "normalize_text"]
