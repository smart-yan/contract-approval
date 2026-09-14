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

⚠️ 已知取舍：表格行与普通段落在数据结构上不再可区分。**P6-1 刻意不做区分** ——
要不要给它们不同的类型，取决于 P7 条款识别怎么用，现在决定就是猜。
"""

from __future__ import annotations

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

#: 表格行内单元格之间的连接符（制表符，与文本抽取工具的惯例一致）
_CELL_SEPARATOR = "\t"


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
            Paragraph(index=i, text=text) for i, text in enumerate(self._iter_block_texts(document))
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
    def _iter_block_texts(self, document: DocxDocument) -> Iterator[str]:
        """按**文档顺序**产出正文段落与表格行的文本。

        顺序很关键：P7 要靠段落顺序还原"上下文"，乱序会让条款与金额对不上。
        """
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                yield DocxParagraph(child, document).text.strip()
            elif child.tag == qn("w:tbl"):
                for row in Table(child, document).rows:
                    yield _CELL_SEPARATOR.join(cell.text.strip() for cell in row.cells)


__all__ = ["DocxParser"]
