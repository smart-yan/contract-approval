"""统一风险项 ``AgentRiskItem``（P9-8）。

它是什么
-------
**同类风险的唯一形状**。上游有**两个互不相识**的生产者：

::

    RuleRisk（P8-1，确定性规则求值）
    ResolvedFinding（P9-4，模型发现 + Agent 定位）
              │  app/risk/unify.py 的两个映射函数
              ▼
        AgentRiskItem —— 合并（后续）、评分（后续）、落库（P10）都只认它

没有它，下游每加一个消费者就要写一遍"规则来源这样、模型来源那样"的分支；
**有了它，"来源"退化成字段 ``source`` 而不是两套代码路径**。

⚠️ 本模块只定义形状，不产出也不合并
--------------------------------
``RULE`` 与 ``LLM`` 两条来源各自映射成同一种形状，**但不在这里相遇**：
这个类只管"一条风险长什么样"。

``RULE+LLM`` 由 :func:`app.risk.merge.merge_risk_items` 产出（P9-9）——
那是**另一个模块**的事，本模块不引入也从不构造它。

字段口径（与 Backend ``risk_item`` 的对齐关系）
--------------------------------------------
========================  ==============================================
``source``                来源；由 **Agent** 决定，模型无权自报
``risk_code``             规则来源 = ``rule_code``；模型来源为 ``None``
``risk_title`` / ``risk_level`` / ``reason`` / ``legal_basis``
                          两侧同义，原样搬运
``dimension``             审查维度。⚠️ **模型来源暂时为 ``None``**（见下）
``original_text``         证据所在的**段落原文**
``quote``                 其中的**命中片段**（一定是 ``original_text`` 的子串）
``paragraph_index``       段落序号（P6-2 位置契约）
``anchor_method``         这次定位**是怎么得到的**；规则来源为 ``None``
``related_rule_code``     模型声称"这条与某条已命中规则是同一件事"；规则来源为 ``None``
========================  ==============================================

两个必须记住的"同义"
------------------
1. **两种来源的 ``original_text`` 都是"段落原文"、``quote`` 都是"证据片段"** ——
   这不是巧合，是 P9-0 专门裁决过的口径。⚠️ 它与 Backend ``risk_item.original_text``
   的语义**不同**（那边指的是"命中片段"，等于这里的 ``quote``）：
   落库（P10）时必须**显式映射**，不能按同名字段对拷。
2. **``anchor_method`` 的语义不能被搬运破坏**：规则风险有确定的段落号，
   **不存在**"反查方式"，因此是 ``None``；只有模型风险（经过 quote 定位）
   才带它是 ``CLAUSE_SCOPED`` / ``CLAUSE_FALLBACK``。给它编一个值
   （例如"规则也算 CLAUSE_SCOPED"）会让"这次定位可信吗"这个信号失真。

``dimension``：缺口已补（P9-8a）
----------------------------
P9-8 曾把它声明为 ``str | None``，因为当时 ``LLMFinding`` **契约里根本没有维度**，
而 Backend ``risk_item.dimension`` 是 ``NOT NULL``（P9-8 如实留空、没有编造，
把这个缺口显式暴露了出来）。

P9-8a 按方案 A 补上了上游：``LLMFinding.dimension`` 成为**必填**字段，
取值受固定词表 ``RiskDimensionLiteral`` 约束（词表取自架构文档 §11.1 的「维度」列，
不是新造的）。两侧因此都能保证有值，这里的类型**收紧为 ``str``** ——
不留无意义的 Optional。

⚠️ 词表的**已知窄口**（写在 ``app/llm/findings.py`` 的 ``RiskDimensionLiteral`` 旁边）：
规则侧的维度在数据库里是自由文本，管理员可以写一个不在那 10 项里的值。

字段的**刻意缺席**
----------------
* ``suggestion`` —— 修改建议是**独立资源**（Backend 有 ``risk_suggestion`` 表），
  不是风险项的列；模型内联的建议仍留在 ``state["llm_findings"]`` 里供建议生成阶段取用
* ``context_before`` / ``context_after`` / ``occurrence_hint`` —— 定位的**输入**，
  定位完成后使命就结束了
* ``clause_index`` / ``clause_id`` —— 合并时可由 ``paragraph_index`` + Clause 区间
  **确定性推导**（P9-0 裁决），不必作为持久字段多存一份可能漂移的真相
* ``confidence`` / ``anchor_score`` / ``char_*`` / 复核状态 / 任何数据库身份
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.constants import RiskSource


class AgentRiskItem(BaseModel):
    """统一风险项 —— 两个来源汇聚后的**唯一形状**。

    ⚠️ 它**不是** Backend ``risk_item`` 的副本：没有 ``task_id`` / ``contract_id`` /
    ``clause_id`` / ``rule_id`` / 字符偏移 / 页码 / 定位方式 / 复核状态 ——
    那些由 Backend 在落库时补齐（FK 解析、文件级属性、人工审核）。
    """

    source: RiskSource = Field(description="风险来源。**由 Agent 决定**，模型无权自报")
    risk_code: str | None = Field(
        default=None, description="规则来源 = ``rule_code``；模型来源为 ``None``（模型没有稳定编码）"
    )
    risk_title: str = Field(min_length=1, description="风险标题（展示用）")
    dimension: str = Field(
        min_length=1,
        description="**风险所属的审查维度**。两侧都必须有值："
        "规则来源取自规则目录的 ``dimension``；"
        "模型来源取自 ``LLMFinding.dimension``（P9-8a 起成为必填，"
        "取值受固定词表约束）。⚠️ 它**不是**条款类型 —— 不许用 ``clause_type`` 反推",
    )
    risk_level: str = Field(
        min_length=1, description="风险等级，取值见 constants.RiskLevel（HIGH / MEDIUM / LOW）"
    )
    reason: str = Field(min_length=1, description="风险成因")
    legal_basis: str | None = Field(default=None, description="法律依据")
    original_text: str = Field(description="证据所在的**段落原文**（注意与 Backend 同名字段的语义差异）")
    quote: str = Field(
        min_length=1,
        description="命中证据片段，**一定是 ``original_text`` 的子串**。"
        "空证据的风险项没有意义 —— 每一项都必须能指回原文",
    )
    paragraph_index: int = Field(ge=0, description="段落序号（P6-2 位置契约）。本轮**不引入字符偏移**")
    anchor_method: str | None = Field(
        default=None,
        description="定位方式。规则来源为 ``None``（它有确定的段落号，不存在反查）；"
        "模型来源为 ``CLAUSE_SCOPED`` / ``CLAUSE_FALLBACK``（见 understanding.locator）",
    )
    related_rule_code: str | None = Field(
        default=None,
        description="模型声称与本条同属一件事的那条规则（**已经过 rule snapshot 核对**，"
        "核不上的在 P9-4 就被清空了）。规则来源为 ``None`` —— 它本身就是规则风险",
    )


__all__ = ["AgentRiskItem"]
