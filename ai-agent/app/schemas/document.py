"""文档解析相关的数据契约。

分层原则
-------
* :class:`Paragraph` —— 文档里的一段内容。**只表达"有什么"，不表达"是什么"** ——
  它不认识"条款""风险""元数据"，那些属于 P7 及之后的阶段
* :class:`ParseResult` —— **一次解析的完整结果**（成功或失败都在里面）

为什么失败也放在 ``ParseResult`` 而不是抛异常
-------------------------------------------
"这份 DOCX 打不开"是可预期的结果，不是程序 bug。把它表达成结果而不是异常，
下游（Node / Graph）才能把它当成数据来处理与传递；
抛异常会中断整张图，而 Graph 的规矩是"失败写进 State，由 Conditional Edge 分流"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Paragraph(BaseModel):
    """文档正文中的一段内容（保持文档顺序）。"""

    index: int = Field(description="在本文档 paragraphs 列表中的顺序，从 0 开始")
    text: str = Field(description="段落文本，已去除首尾空白")


class ParseResult(BaseModel):
    """一次文档解析的产出。

    ``status`` 的三种取值
    -------------------
    ==========  ==========================================================
    ``PARSED``  解析成功，且拿到了非空正文
    ``EMPTY``   解析成功，但正文是空的（空文档 / 只有空白段落）
    ``FAILED``  没能解析出内容，原因见 ``error_code`` / ``error_message``
    ==========  ==========================================================

    ``EMPTY`` 与 ``FAILED`` 必须分开：前者是"文档本身没内容"（数据问题，
    下游应跳过而不是重试），后者是"我们没能读出来"（系统问题，可能需要人工介入）。
    """

    status: Literal["PARSED", "EMPTY", "FAILED"] = Field(description="解析结论")
    parser: str = Field(description="产出该结果的解析器标识，用于排查与统计")
    source_file_type: str = Field(description="Backend 识别出的文件类型：DOCX / PDF / JPEG / PNG")

    text: str = Field(default="", description="文档完整纯文本，由 paragraphs 按顺序拼接")
    paragraphs: list[Paragraph] = Field(default_factory=list, description="段落结构，保持文档顺序")

    #: 仅 ``status == FAILED`` 时有值，取值见 :class:`~app.core.errors.AgentErrorCode`
    error_code: str | None = Field(default=None, description="失败原因码；成功时为 None")
    error_message: str | None = Field(default=None, description="人话失败原因；成功时为 None")


__all__ = ["Paragraph", "ParseResult"]
