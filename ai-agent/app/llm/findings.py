"""LLM 审查的**领域契约**（P9-2）：模型该吐出什么、提示该喂给它什么。

分层
----
========================  ======================================================
``schemas.py``            通用调用契约（``LLMRequest`` / ``LLMResult``）—— **不认识合同**
``findings.py``（本文件）  ``clause_review`` 场景的输入/输出契约 —— **业务形状**
``prompts.py`` + ``prompts/*.md``  提示文本与渲染（版本化）
========================  ======================================================

**结构定义只有这一份**：后续 JSON Schema 由 :meth:`BaseModel.model_json_schema`
自动产生（见 ``json_guard.json_schema_for``），注入提示的那段也由同一个模型生成。
手抄第二份 JSON Schema 是这类系统最经典的漂移源。

⚠️ 为什么 Finding 里**没有** ``paragraph_index``
---------------------------------------------
段落序号是 **Agent 的内部坐标系**（P6-2 契约，由解析器产生，同一份文件 + 同一个
parser 版本下才稳定）。模型看到的是**拼装后的提示文本** —— 它对"第几段"没有任何
可靠观测。让它输出段落号，等于让它猜一个它没有的坐标系，而且这个猜测**无法与真实
文档核对**（猜错了看起来也像真的）。

模型只负责它能负责的两件事：

* **范围标签** ``clause_index`` —— 由 Agent 注入提示、模型原样回显，用来把搜索范围
  缩到一条条款内（Agent 会校验范围，越界即丢弃）
* **原文证据** ``quote`` —— 必须逐字复制输入文本

最终坐标由 Agent 在该条款范围内用 ``quote`` 定位得出（P9-3），**不由模型给出**。

⚠️ ``source`` / ``risk_code`` 也不在这里
--------------------------------------
* ``source`` 由 Agent 侧统一决定（规则风险 = ``RULE``，模型风险 = ``LLM``，
  合并后 = ``RULE+LLM``）。**绝不接受模型自报来源** —— 那是 Agent 的记录职责，
  让模型来填只会引入一个无法核对的字段。
* ``risk_code`` 在统一风险模型里允许为 ``None``（模型没有稳定编码），
  更不该让模型自己编一个。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 风险等级取值（与 ``backend/app/core/constants.py`` 的 ``RiskLevel`` 一致）。
#: 用 ``Literal`` 而不是 ``str``：它会渲染进 JSON Schema 的 ``enum``，
#: 模型在提示里**看得到**合法取值，越界值也会在 Pydantic 校验时被直接拒绝。
RiskLevelLiteral = Literal["HIGH", "MEDIUM", "LOW"]

#: 建议类型（与 Backend 的 ``SuggestionType`` 一致：REPLACE / ADD / DELETE）。
#: ⚠️ Backend 侧新增取值时这里要同步 —— 它同时是"模型被允许输出的取值集合"。
SuggestionTypeLiteral = Literal["REPLACE", "ADD", "DELETE"]

#: **风险所属的审查维度**的固定词表（P9-8a 引入）。
#:
#: 取值**直接取自架构文档 §11.1 的「维度」列**（10 项）—— 不是在这里新造的：
#: 那些名字在文档里与 ClauseType 并列出现过（如「知识产权 IP」「金额支付 AMOUNT_PAYMENT」），
#: 并且当前 seed 的 3 条规则用的就是其中的 3 个。本步只是把**既有词表**写成了契约。
#:
#: ⚠️ **维度 ≠ 条款类型**：文档里「条款完备性」这一维度**没有**对应的 ClauseType
#: （它说的是"必备条款缺失"这件事本身），足以说明两者不是一回事。
#: 因此**绝不允许**用 ``clause_type`` 反推 dimension（那会把"争议管辖"变成 "DISPUTE"）。
#:
#: ⚠️ 已知窄口（如实记录，不在本步解决）：
#: * 文档的 ``DELIVERY``（交付）条款类型**没有**对应的维度名 ——
#:   若模型认为问题属于"交付"，它只能在现有 10 项里挑一个最接近的，或干脆不报
#: * 规则侧的 ``dimension`` 在数据库里是**自由文本**（``String(32)``，无约束、无枚举），
#:   管理员可以在规则目录里写一个不在这 10 项里的维度 —— 那时两侧的维度对不上，
#:   合并（P9-9）按维度分桶时要考虑这个不对称
RiskDimensionLiteral = Literal[
    "主体资质",
    "金额支付",
    "违约责任",
    "知识产权",
    "争议管辖",
    "保密",
    "不可抗力",
    "数据安全",
    "验收",
    "条款完备性",
]


# --------------------------------------------------------------------------- #
# 模型输出（LLM Review 的 findings 契约）
# --------------------------------------------------------------------------- #
class Suggestion(BaseModel):
    """模型内联给出的修改建议。

    §9.2 2e：建议生成**优先取它**；取不到才回退到 ``rule.suggestion_template``。
    因此它可以缺席（``LLMFinding.suggestion`` 为 ``None``），契约不强制模型每次都写。
    """

    type: SuggestionTypeLiteral = Field(description="建议类型：替换 / 新增 / 删除")
    text: str = Field(description="建议的示范文本")
    reason: str | None = Field(default=None, description="为什么这么改（可选）")


class LLMFinding(BaseModel):
    """模型报出的**一条**风险。

    字段与 P9-0 批准的契约一致。刻意**没有**：``paragraph_index``（内部坐标系，
    见模块 docstring）、``source``（Agent 决定）、``risk_code``（模型没有稳定编码）、
    字符偏移、置信度（``confidence`` 由调用侧另行记录，不在模型输出契约里）。
    """

    clause_index: int = Field(
        ge=0,
        description="**范围标签**，必须原样回显输入里标注的条款编号。"
        "⚠️ 它不是最终坐标；Agent 会校验它是否越界，越界的 finding 直接丢弃",
    )
    dimension: RiskDimensionLiteral = Field(
        description="**风险所属的审查维度**，只能取固定词表里的值（见 RiskDimensionLiteral）。"
        "⚠️ 它与 ``clause_index`` 指向的**条款类型不是一回事**：维度说的是"
        "「这属于哪一类审查关注点」，不是「它写在哪一条里」。"
        "越界/自造的维度会被 schema 校验直接拒绝（P9-1 的严格校验，不做修复）",
    )
    risk_title: str = Field(min_length=1, description="风险标题（简短、可读，将作为风险卡片的标题）")
    risk_level: RiskLevelLiteral = Field(description="风险等级，只能取 HIGH / MEDIUM / LOW")
    reason: str = Field(min_length=1, description="风险成因：为什么这对合同一方不利")
    legal_basis: str | None = Field(
        default=None, description="法律合规依据（如《民法典》条文）；不确定时留空，**不要编造条文号**"
    )
    quote: str = Field(
        min_length=1,
        description="**逐字复制**输入条款文本中的片段，作为命中证据。"
        "禁止改写、禁止用省略号截断。它是 Agent 定位与人工核对的唯一依据"
        "（§10.5 的『长度 < 4 即丢弃』属于定位阶段的判定，不在本 schema 里）",
    )
    context_before: str = Field(
        default="",
        max_length=30,
        description="quote 紧邻的前文（逐字复制，最多 30 字），供消歧用。"
        "**允许为空** —— quote 可能正好在段落开头",
    )
    context_after: str = Field(
        default="",
        max_length=30,
        description="quote 紧邻的后文（逐字复制，最多 30 字），供消歧用。"
        "**允许为空** —— quote 可能正好在段落结尾",
    )
    occurrence_hint: int | None = Field(
        default=None, ge=1, description="模型认为这是第几次出现（1-based）。**仅作参考**，不作唯一依据"
    )
    suggestion: Suggestion | None = Field(default=None, description="内联修改建议（可选）")
    related_rule_code: str | None = Field(
        default=None,
        description="若这条 finding 与某条**已命中的规则**是同一件事，回填那条规则的 rule_code。"
        "⚠️ 只能从提示里给出的规则代码中选；Agent 事后会拿它到本次 rule snapshot 里核对，"
        "**对不上的一律作废**（不能凭它建立任何关联）。不确定时留空 —— 留空是安全的选择",
    )


class LLMReviewResult(BaseModel):
    """一次 clause_review 调用的**顶层输出对象**。

    为什么要包一层：``response_format={"type": "json_object"}` 要求顶层是 **JSON 对象**，
    裸数组不合法。包一层不是风格选择，是协议约束。
    """

    findings: list[LLMFinding] = Field(
        default_factory=list,
        description="本批条款里发现的风险；没有发现就是空数组（**允许为空**，不要为了凑数报风险）",
    )


# --------------------------------------------------------------------------- #
# 提示输入（Agent → 模型）
# --------------------------------------------------------------------------- #
class ClauseContext(BaseModel):
    """喂给模型的**一条**条款。

    只带 Agent 侧真实存在的标识：``clause_index`` 是文档内序号，
    **不是** Backend 落库后的 ``clause_id``（Agent 按设计不持有任何数据库主键）。
    """

    clause_index: int = Field(ge=0, description="条款在本文档内的序号（Agent 的坐标系，模型只回显）")
    clause_type: str = Field(description="条款类型，取值见 core.constants.ClauseType")
    clause_no: str | None = Field(default=None, description="编号原文，如「第三条」")
    title: str | None = Field(default=None, description="条款标题")
    text: str = Field(description="条款全文 —— 模型**只能**依据它作判断，不得引入外部信息")


class MatchedRuleHint(BaseModel):
    """**规则已命中的提示**（§9.2 2b 的上下文注入）。

    两个作用：让模型**不重复报告**规则已经抓到的确定性风险（专注语义问题）；
    给模型提供 ``related_rule_code`` 的**可选项**。

    ⚠️ ``rule_code`` 必须在提示里出现：没有它，``related_rule_code`` 就没有可选项，
    模型只能编一个 —— 而 Agent 事后要拿它去 rule snapshot 里核对，编的必然作废，
    等于白让模型输出一个字段。
    """

    rule_code: str = Field(
        min_length=1, description="规则编码 —— 模型回填 related_rule_code 时的**唯一合法取值来源**"
    )
    rule_name: str = Field(description="规则名称，帮模型判断「是不是同一件事」")
    dimension: str = Field(description="审查维度")
    risk_level: str = Field(description="这条规则命中后的风险等级")
    quote: str = Field(description="规则命中到的证据片段 —— 模型据此判断自己报的是不是同一条")


class ClauseReviewPromptInput(BaseModel):
    """一次 clause_review 提示的**完整输入**。

    §9.2 2b 要求注入"合同类型 + 我方立场 + 该批条款文本 + 规则已命中的提示"。
    其中**我方立场（甲方/乙方）当前没有真实数据源**（Backend 的 ``contract`` 表只有
    ``our_party`` / ``counterparty`` 主体名称，没有"我方是甲方还是乙方"这个字段；
    Agent 的上传请求里也没有）—— 因此**契约里不留这个位置**：
    一个没有数据源的字段只会变成永远为空的幽灵字段，读代码的人却会以为它有人填。
    审查视角目前**只由 ``contract_type`` 决定**（采购合同即以采购方为我方）。

    数据源将来若确定（Backend 加字段或上传请求带上），再加字段并同步提示版本。
    """

    contract_type: str = Field(description="合同类型，取值见 core.constants.ContractType —— **决定审查视角**")
    clauses: list[ClauseContext] = Field(
        min_length=1, description="本批条款（§9.2：按 clause_type 聚合，≤ 8 条）"
    )
    matched_rules: list[MatchedRuleHint] = Field(
        default_factory=list, description="规则已命中的提示；没有就为空数组"
    )


__all__ = [
    "ClauseContext",
    "ClauseReviewPromptInput",
    "LLMFinding",
    "LLMReviewResult",
    "MatchedRuleHint",
    "RiskDimensionLiteral",
    "RiskLevelLiteral",
    "Suggestion",
    "SuggestionTypeLiteral",
]
