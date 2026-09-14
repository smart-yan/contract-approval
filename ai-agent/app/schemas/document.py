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

位置定位契约（P6-2 收口）
=======================

**全系统引用文档来源位置，统一用「段落序号」，不用字符偏移。**

::

    risk_item.paragraph_index  ──▶  ParseResult.paragraphs[paragraph_index]
                                            │
                                            └─ .text  ← 用来核对"引用的确实是这一段"

三条支撑这个契约的不变量
----------------------

1. **``paragraphs`` 保持文档顺序**，``index`` 从 0 连续递增，等于它在列表中的位置。
   解析是纯函数（同一份文件 + 同一个 ``parser`` ⇒ 同一份结果），
   所以 ``index`` 在**同一份文件与同一个解析器版本**下是稳定的。
   ``ParseResult.parser`` 记录了这份结果出自哪个解析器 —— 解析行为一旦改变，
   历史索引的解释要连同它一起看。

2. **``text`` 与 ``paragraphs`` 互为可还原**：``text == "\\n".join(p.text for p in paragraphs)``，
   且段落文本内不含 ``\\n``。这条由解析器保证（见 ``app/parsers/docx_parser.py``
   的清洗规则），它让"拿到一段文本、想知道它在第几段"永远有确定答案。

3. **空段落保留**。空白段落是文档结构的真实组成部分，丢掉它们会让后面的段落
   序号整体前移、与原文对不上。清洗后它的 ``text`` 是空串，但不消失。

分阶段怎么用
-----------
======================  ====================================================
P7 条款识别             在 ``paragraphs`` 上切分；产出的条款记录引用段落序号区间
P9 风险结果             每条风险记录引用一个段落序号（+ 原文片段用于核对）
P11 前端来源高亮        按段落序号定位到对应段落并高亮
======================  ====================================================

**为什么现在不做字符级偏移**
黄金链路的定位精度定在"block/clause 级 + 段落索引"（架构文档 §17.3）。
全局字符偏移（§10.2 统一坐标系）是完整设计的目标，但它要等 P7/P9 真正需要
"段落内的更细定位"时再引入 —— 届时偏移可以由不变量 2 **无歧义地推出**，
这正是本轮把 ``\\n`` 歧义修掉的原因。现在加只会多一个没人用的字段。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Paragraph(BaseModel):
    """文档正文中的一段内容（保持文档顺序）。"""

    index: int = Field(
        description="在本文档 paragraphs 列表中的顺序，从 0 起连续递增。"
        "**这就是全系统引用文档位置的标识**（见模块 docstring 的位置定位契约）。"
        "同一份文件 + 同一个 parser 版本下稳定"
    )
    text: str = Field(
        description="段落文本，已按统一的清洗规则压成**单行**：连续空白折叠为一个空格、"
        "去掉首尾空白、**不含换行与制表符**。空段落（只有空白）清洗后是空串，但不会被丢弃"
    )


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

    ``text`` 与 ``paragraphs`` 的关系
    ------------------------------
    ``text`` **恒等于** ``"\\n".join(p.text for p in paragraphs)``，且段落文本内不含 ``\\n``
    —— 因此每个 ``\\n`` 都是一个段落边界，两者互为可还原。
    ``FAILED`` 时 ``text`` 与 ``paragraphs`` 都是空的。
    """

    status: Literal["PARSED", "EMPTY", "FAILED"] = Field(description="解析结论")
    parser: str = Field(
        description="产出该结果的解析器标识，用于排查与统计，"
        "同时也界定了 index 的稳定性前提（同一份文件 + 同一个 parser ⇒ 同一份结果）"
    )
    source_file_type: str = Field(description="Backend 识别出的文件类型：DOCX / PDF / JPEG / PNG")

    text: str = Field(
        default="",
        description="文档完整纯文本，恒等于 paragraphs 的 text 以 \\n 拼接。"
        "**不含**段落内换行，因此段落边界可从它还原",
    )
    paragraphs: list[Paragraph] = Field(
        default_factory=list,
        description="段落结构，保持文档顺序；index 从 0 连续递增。"
        "P7 的条款识别、P9 的风险定位、P11 的原文高亮都以它为基准",
    )

    #: 仅 ``status == FAILED`` 时有值，取值见 :class:`~app.core.errors.AgentErrorCode`
    error_code: str | None = Field(default=None, description="失败原因码；成功时为 None")
    error_message: str | None = Field(default=None, description="人话失败原因；成功时为 None")


__all__ = ["Paragraph", "ParseResult"]
