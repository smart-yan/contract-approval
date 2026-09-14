"""理解层的数据契约（P7）。

与 ``schemas/document.py`` 的分工
-------------------------------
* ``document.py`` —— **文档本身**：原文被解析成了哪些段落、在哪
* ``understanding.py`` —— **对文档的理解**：段落被组织成了哪些条款、是什么类型

两者是"原料"与"加工结果"的关系：理解层的每一个结论都必须能指回文档层的段落序号。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Clause(BaseModel):
    """一个条款 —— 由**连续的若干段落**组成。

    位置契约（沿用 P6-2）
    -------------------
    条款的位置由一对**闭区间**的段落序号表达：``[start_paragraph_index, end_paragraph_index]``
    （两端都含）。不引入字符偏移、不出现任何数据库主键 —— 落库时由 persist 层
    把段落序号映射成 ``document_block.id`` 与全局偏移。

    所有条款的区间构成段落序列的一个**完整划分**（无重叠、无遗漏），
    因此"某个段落属于哪个条款"永远有唯一答案。
    """

    clause_index: int = Field(
        description="**本文档内**的连续索引，从 0 起递增（前言也占一个号）。"
        "⚠️ 它只是文档内的序号，**不是跨文档稳定的 ID** —— 换一份合同、"
        "或换一个切分版本，同一个索引指向的可能是完全不同的条款"
    )
    clause_no: str | None = Field(
        description="识别出的编号**原文**（如「第三条」「第 1 条」）；"
        "前言与无编号文档为 None。保留原文而不做归一化 —— 它是文档里真实写着的东西"
    )
    title: str | None = Field(
        description="条款标题，**只从编号段派生**：编号段去掉编号后，剩下的非空文字就是标题"
        "（如「第一条 合同标的与金额」→ title = 合同标的与金额）。"
        "编号段只写了编号（如「第一条」）时为 None；"
        "**是否跨多段与它无关** —— 单段条款一样可以有标题。"
        "前言与无编号文档为 None。不去后续正文段落里找标题"
    )
    clause_type: str = Field(
        description="条款类型，取值见 ``app.core.constants.ClauseType``。"
        "由**编号段**的标题文字判定，不扫描条款全文（见 understanding/clauses.py）"
    )
    start_paragraph_index: int = Field(description="起始段落序号（含）")
    end_paragraph_index: int = Field(description="结束段落序号（含）")
    text: str = Field(
        description="条款全文，**严格由段落文本派生**："
        '`"\\n".join(p.text for p in paragraphs[start:end+1])`。'
        "不做二次清洗、不重新拼装、不改写表格行文本"
    )
    extract_method: str = Field(
        description="切分方式，取值见 ``app.core.constants.ExtractMethod``；P7-1 恒为 RULE"
    )


class MetadataItem(BaseModel):
    """一条从文档里抽出来的元数据。

    边界（与 Backend 的分工）
    ----------------------
    这里抽的是**文档内容里写着的事实**，不是 Backend 上的合同业务字段。
    合同编号 / 名称 / 类型 / 送审部门这些由用户在上传时声明，属 Backend 的主数据 ——
    Agent 不从文档里再猜一遍（猜错会静默改变规则集的选择）。

    位置契约（沿用 P6-2）
    -------------------
    ``paragraph_index`` + ``quote`` 回答"这个值是从哪句话读出来的"。
    不引入字符偏移、不出现数据库主键。
    """

    field_key: str = Field(description="字段键，如 counterparty_name / contract_amount")
    field_label: str = Field(description="展示名，来自提取器的字段目录（一处定义）")
    field_value: str = Field(
        description="字段值。**已规范化**：日期统一 YYYY-MM-DD、金额统一两位小数的纯数字串。"
        "类型由 value_type 标明 —— 规范化让下游不必各自解析一遍"
    )
    value_type: str = Field(description="TEXT / AMOUNT / DATE / CODE")
    paragraph_index: int = Field(description="来源段落序号。**块级字段**（如付款条件）指向该块的**第一段**")
    quote: str = Field(
        description="命中的原文片段 —— 人工核对与前端展示的依据。"
        "绝大多数字段是单段文本；**块级字段可能跨连续多段**，"
        "此时它按 \\n 拼接，行 i 对应段落 ``paragraph_index + i``"
    )
    extract_method: str = Field(
        description="提取方式，取值见 ``app.core.constants.ExtractMethod``；"
        "确定性抽取恒为 REGEX（Backend 侧标注该值用于元数据字段）"
    )


__all__ = ["Clause", "MetadataItem"]
