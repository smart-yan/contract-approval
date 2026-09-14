"""文档解析相关的数据契约。

⚠️ P5-3 只有桩实现
-----------------
``ParseResult`` 的形状是为 P7 的真实 Parser（``ai-agent/app/parsers/``）预留的，
但 P5-3 **不会**填入任何真实合同正文 —— ``text`` 恒为空串，这是刻意的：
伪造一段看起来像合同的文本，比留空更危险。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ParseResult(BaseModel):
    """一次文档解析的产出。"""

    status: Literal["STUB"] = Field(
        default="STUB",
        description="P5-3 恒为 STUB。真实解析（DOCX / PDF / OCR）属于 P7，届时会扩展该枚举",
    )
    parser: str = Field(description="产出该结果的解析器标识")
    source_file_type: str = Field(description="Backend 识别出的文件类型：DOCX / PDF / JPEG / PNG")
    text: str = Field(
        default="",
        description="文档正文。**P5-3 桩实现恒为空串** —— 刻意不伪造合同文本",
    )


__all__ = ["ParseResult"]
