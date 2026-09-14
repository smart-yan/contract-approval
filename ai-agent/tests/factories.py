"""测试用的最小文档工厂。

**不依赖仓库外的样例文件** —— 每个用例自己现场生成，所以测试在任何机器上都能跑，
也不会因为样例文件被改动而悄悄失效。
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from pathlib import Path

import docx

#: 一个 block 要么是一段文字（段落），要么是一张表（行 → 单元格）
Block = str | list[list[str]]


def save_docx(path: Path, *blocks: Block) -> Path:
    """按给定顺序生成一个真实 DOCX 并写到 ``path``，返回该路径。

    ``str``  → 一个段落
    ``list`` → 一张表，外层是行、内层是单元格文本
    """
    document = docx.Document()
    for block in blocks:
        if isinstance(block, str):
            document.add_paragraph(block)
        else:
            _add_table(document, block)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    return path


def docx_bytes(*blocks: Block) -> bytes:
    """同 :func:`save_docx`，但返回字节流（用于构造 multipart 上传体）。"""
    buffer = io.BytesIO()
    document = docx.Document()
    for block in blocks:
        if isinstance(block, str):
            document.add_paragraph(block)
        else:
            _add_table(document, block)
    document.save(buffer)
    return buffer.getvalue()


def _add_table(document: docx.Document, rows: Iterable[Iterable[str]]) -> None:
    rows = [list(row) for row in rows]
    if not rows:
        return
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    for r, row in enumerate(rows):
        for c, cell_text in enumerate(row):
            table.cell(r, c).text = cell_text


__all__ = ["Block", "docx_bytes", "save_docx"]
