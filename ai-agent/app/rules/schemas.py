"""P8 规则求值的 Agent 侧数据契约（P8-1）。

契约链
-----
::

    Backend GET /api/v1/rule-sets            （P8-0：只读规则目录）
              │  rule_from_backend()
              ▼
          AgentRule                          （Agent 自己的规则模型，无数据库身份）
              │  evaluate_rule(rule, clauses) （evaluator.py，纯函数）
              ▼
      RuleEvaluationResult                   （MATCHED / NOT_MATCHED / EVALUATION_FAILED）
              │  .risks
              ▼
           RuleRisk                          （最小风险结果，source 恒为 RULE）

为什么 Agent 不直接用 Backend 的 ``RuleItem``
------------------------------------------
``backend/app/schemas/rule.py`` 的 ``RuleItem`` 是 Backend 的**资源读模型**：
它带 ``id``，而 ``id`` 是 Backend 的资源标识，对"这条规则是否命中"没有任何贡献。
Agent 若把它当内部核心模型，数据库主键会顺着规则对象渗进求值结果、渗进 State、
渗进将来要落库的 payload —— 而两侧之间是 HTTP 边界，规则数据归 Backend、
求值引擎归 Agent（架构文档 §17 的 P8 行）。

所以这里定义 **Agent 自己的规则模型**：字段口径与 Backend **一致**
（那边是数据源，口径不能各自发明），但不含任何数据库身份。
两侧唯一的转换路径是 :func:`rule_from_backend`。

⚠️ 本轮**不修改** P8-0 的 Backend 契约 —— 映射只发生在 Agent 这一侧。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AgentRule(BaseModel):
    """一条规则在 Agent 内部的形态。

    字段与 ``backend/app/schemas/rule.py::RuleItem`` **一一对应（去掉 ``id``）**，
    取值口径全部**原样搬运**：Agent 不把 ``dimension`` 映射成枚举码、不归一化
    ``expression``、不改写 ``severity`` —— 规则目录是 Backend 的数据，
    解释权在求值器，不在这一层。
    """

    rule_code: str = Field(description="规则编码（业务编码，跨版本稳定）。求值结果的追溯键")
    rule_name: str = Field(description="规则名称。命中后作为风险标题")
    dimension: str = Field(description="审查维度。**原样搬运**（当前 Backend seed 用的是中文展示名）")
    description: str | None = Field(default=None, description="规则说明")
    rule_type: str = Field(
        description="求值器类型，取值见 ``core.constants.RuleType``。"
        "⚠️ 刻意是 ``str`` 而不是枚举：``rule_type`` 由 Backend 的规则目录配置，"
        "收成枚举就等于让一个配错的值在**构造规则**时抛错，把"
        "「一条规则算不出来」升级成「整份规则集加载失败」。"
        "要的是前者 —— 见 evaluator 的 UNSUPPORTED_RULE_TYPE"
    )
    expression: dict[str, Any] = Field(
        description="求值表达式。形状由 ``rule_type`` 决定，解释权在 evaluator；这一层不做任何校验与归一化"
    )
    target_clause_types: list[str] | None = Field(
        default=None,
        description="限定作用的条款类型；``None`` 或**空列表**都表示不限（全文档生效）",
    )
    severity: str = Field(description="命中后的风险等级。原样搬运 → ``RuleRisk.risk_level``")
    legal_basis: str | None = Field(default=None, description="法律依据。原样搬运 → ``RuleRisk.legal_basis``")
    suggestion_template: str | None = Field(
        default=None,
        description="修改建议模板。P8-1 **不消费它**（建议的生成与合并属后续阶段），"
        "保留是因为它本来就是规则的一部分，落在这里可以省掉 P8-2 回头再查一次 Backend",
    )
    sort_order: int = Field(description="展示与执行顺序。P8-1 不求值它，只为保持与数据源同形")


class RuleSetSnapshot(BaseModel):
    """某合同类型下**当前启用**的规则集快照 —— Agent 求值的规则来源。

    ::

        RuleSetSnapshot
        ├── contract_type
        ├── rule_set_version
        └── rules[]  ──▶  AgentRule …

    ``rule_set_version`` 为 ``None`` 等价于 Backend 的 ``rule_set=null``：
    **该合同类型没有配置启用的规则集**。这**不是错误**（架构裁决：
    没有规则集不能阻止上传），因此快照仍是一个合法对象，``rules`` 为空列表，
    流程照常继续 —— 只是这一份合同没有任何规则可跑。

    ⚠️ ``version`` 在这里而不是在 :class:`AgentRule` 上：它是**规则集这一层**的属性
    （这一批规则作为一个整体是哪个版本），不是某条规则的属性。
    它也不是给求值器用的，而是给下游记录"这次审查依据的是哪一版规则"用的。
    本轮**不消费它**（P8-2 的任务幂等键才会用到）。

    ⚠️ Backend 的 ``rule_set.id`` **不进来**：Agent 不需要数据库身份，
    规则集的身份由 ``contract_type`` + ``rule_set_version`` 表达。
    ``rule_set.name`` / ``description`` 同理不收 —— 没有消费者。

    领域不变量（下面校验器强制）
    --------------------------
    ==================================  ==========================================
    ``rule_set_version is None``         ⇔ **没有规则集** ⇒ ``rules`` 必须为空
    ``rule_set_version`` 有值            ⇒ 必须是非空字符串（有规则集就有版本）
    ==================================  ==========================================

    为什么这两条必须成立：``rule_set_version is None`` 在下游读作
    "该合同类型没有配置规则集"，是一个**正常结论**。若它同时带着一堆规则，
    同一份快照就自相矛盾 —— 而无论下游往哪边解读（当成"没有规则集"而忽略规则、
    或当成"有规则"而丢掉版本），都是**静默丢信息**。所以宁可在这里失败。
    """

    contract_type: str = Field(description="规则集适用的合同类型，取值见 core.constants.ContractType")
    rule_set_version: str | None = Field(
        default=None,
        description="规则集版本（如 v1）；``None`` 表示该合同类型**没有启用规则集**（此时 rules 必为空）。"
        "Backend 的 ``rule_set.id`` 不进入 Agent",
    )
    rules: list[AgentRule] = Field(
        default_factory=list,
        description="启用规则，**保持 Backend 返回的顺序**（已是 ``sort_order ASC, id ASC``）；"
        "没有规则集时为空列表。Agent 不重排 —— 顺序的权威在 Backend",
    )

    @model_validator(mode="after")
    def _reject_version_rules_mismatch(self) -> RuleSetSnapshot:
        """版本与规则必须互相说得通（见类 docstring 的不变量表）。"""
        if self.rule_set_version is None:
            if self.rules:
                raise ValueError(
                    f"rule_set_version 为 None（= 没有规则集）时 rules 必须为空，实际有 {len(self.rules)} 条"
                )
            return self
        if not self.rule_set_version:
            raise ValueError("rule_set_version 要么为 None（没有规则集），要么是非空字符串")
        return self


class RuleRisk(BaseModel):
    """规则命中产生的一条**最小**风险结果。

    ⚠️ 它不是 Backend ``risk_item`` 的副本。刻意**没有**：``risk_item_id`` /
    ``task_id`` / ``contract_id`` / ``clause_id`` / ``block_id`` / 字符偏移 /
    落库状态 / 人工复核状态 / ``confidence`` —— 那些属于持久化（P10+）与人工审核层，
    由 Backend 在落库时补齐。Agent 在这里只回答"哪条规则、在文档的哪个位置、
    因为什么被触发"。

    ``clause_id`` 之类之所以不能有：Agent 侧根本不存在这些 ID ——
    条款位置由 P7 的契约（段落序号区间）表达，落库时才映射成数据库主键。
    """

    risk_code: str = Field(description="风险编码 = ``AgentRule.rule_code``，用于回溯是哪条规则产出的")
    risk_title: str = Field(description="风险标题 = ``AgentRule.rule_name``")
    dimension: str = Field(description="审查维度。原样来自规则")
    risk_level: str = Field(description="风险等级。原样来自规则的 ``severity``")
    source: Literal["RULE"] = Field(
        default="RULE",
        description="风险来源。P8-1 产出的**恒为 RULE** —— 类型写成 ``Literal`` "
        "而不是 ``str``，是为了让「这里只可能产出规则风险」成为类型约束而不是口头约定",
    )
    reason: str = Field(description="命中理由（人话）。由**规则表达式 + 实际命中的原文**推出，不引入外部信息")
    legal_basis: str | None = Field(default=None, description="法律依据。原样来自规则")
    original_text: str = Field(
        description="命中所在的**段落原文**，取自文档本身。位置契约见 evaluator 的 ``_iter_lines``"
    )
    paragraph_index: int = Field(
        description="命中所在段落序号（P6-2/P7-1 的位置契约）。"
        "**本轮不引入 char offsets** —— 定位到段落，前端据此高亮整段"
    )
    quote: str = Field(description="命中原文的**逐字片段**，是 ``original_text`` 的子串，绝不凭空生成")


class RuleEvaluationStatus(StrEnum):
    """一条规则的求值结论。**三态，不是一个 bool**。

    ===================  ==========================================================
    ``MATCHED``          规则命中，``risks`` 至少有一条
    ``NOT_MATCHED``      规则**求值成功**，且没有命中 —— 这是一个**确定的判断**
    ``EVALUATION_FAILED``规则**没能被求值** —— 表达式不认识 / 非法 / 缺少所需输入
    ===================  ==========================================================

    为什么必须分开后两者
    ------------------
    ``NOT_MATCHED`` 是"我看了，没有"；``EVALUATION_FAILED`` 是"我没法判断"。
    把后者归进前者，等于用"没找到风险"掩盖"这条规则根本没跑"——
    审查报告会因此**静默漏报**，而且没有任何信号提示有人该去补 expression 契约。
    反过来，把它当成"命中"则是凭空造风险。两种误判都不可接受，所以它是第三种状态。

    ⚠️ 本轮**不**再加 ``UNSUPPORTED`` 之类的第四态：无法求值的**原因**由
    :class:`EvaluationFailureReason` 表达，状态数量保持不变。
    """

    MATCHED = "MATCHED"
    NOT_MATCHED = "NOT_MATCHED"
    EVALUATION_FAILED = "EVALUATION_FAILED"


class EvaluationFailureReason(StrEnum):
    """``EVALUATION_FAILED`` 的**原因**（只在这一状态下有值）。

    区分原因的意义：每种原因对应一种完全不同的后续动作 ——
    补求值器 / 修表达式 / 补输入 / 修作用范围配置，
    混成一个字符串就没法据此分派。
    """

    UNSUPPORTED_RULE_TYPE = "UNSUPPORTED_RULE_TYPE"  # 该 rule_type 的求值契约尚未定义
    INVALID_EXPRESSION = "INVALID_EXPRESSION"  # expression 缺失字段 / 结构不对 / 正则非法
    MISSING_INPUT = "MISSING_INPUT"  # expression 合法，但当前输入算不出所需的值（GAP-C）
    INVALID_TARGET_CLAUSE_TYPE = "INVALID_TARGET_CLAUSE_TYPE"  # target_clause_types 含未知条款类型


class RuleEvaluationResult(BaseModel):
    """``evaluate_rule`` 的返回。

    三态的不变量在**构造处**钉死（见下面的校验器）：下游永远看不到
    "命中但没有风险项"或"求值失败却没有原因"这种自相矛盾的结果。
    """

    rule_code: str = Field(description="被求值的规则编码 —— 结果与规则的关联键")
    rule_name: str = Field(description="被求值的规则名称，便于直接展示与排查")
    status: RuleEvaluationStatus = Field(description="求值结论")
    risks: list[RuleRisk] = Field(
        default_factory=list, description="命中产生的风险结果；仅 MATCHED 时非空，顺序与文档顺序一致"
    )
    failure_reason: EvaluationFailureReason | None = Field(
        default=None, description="仅 EVALUATION_FAILED 时有值"
    )
    failure_message: str | None = Field(
        default=None, description="仅 EVALUATION_FAILED 时有值的人话原因（面向排查与规则维护者）"
    )

    @model_validator(mode="after")
    def _reject_inconsistent_status(self) -> RuleEvaluationResult:
        """三态各自只允许一种字段组合。越界即**构造处的代码 bug**，直接暴露。"""
        if self.status is RuleEvaluationStatus.MATCHED:
            if not self.risks:
                raise ValueError("MATCHED 必须至少带一条风险结果")
            if self.failure_reason is not None:
                raise ValueError("MATCHED 不得带 failure_reason")
            return self

        if self.risks:
            raise ValueError(f"{self.status} 不得带风险结果")
        if (self.failure_reason is None) != (self.status is RuleEvaluationStatus.NOT_MATCHED):
            raise ValueError(
                f"{self.status} 的 failure_reason 不匹配：EVALUATION_FAILED 必须有，NOT_MATCHED 必须没有"
            )
        return self


# --------------------------------------------------------------------------- #
# Backend → Agent 契约映射
# --------------------------------------------------------------------------- #
def rule_from_backend(payload: Mapping[str, Any]) -> AgentRule:
    """把 Backend ``RuleItem`` 的字典映射成 :class:`AgentRule`。

    这是两侧规则契约的**唯一转换路径**。

    白名单而非整体透传
    ----------------
    ``payload`` 里 Backend 的 ``id`` 会被挡在外面。用 ``AgentRule(**payload)`` 或
    ``AgentRule.model_validate(payload)``（Pydantic 默认忽略多余字段）也能"跑通"，
    但那是**静默**丢弃：Backend 下次新增 ``created_at`` / ``rule_set_id`` 之类的字段，
    它们会悄悄进入 Agent 的核心模型，而**没有任何测试会失败**。
    这里改成显式白名单 —— 白名单直接取自 :class:`AgentRule` 自己的字段声明，
    因此它不可能与模型漂移（加字段 = 同时改模型与契约），
    Backend 多给的东西则一律进不来。

    ⚠️ 白名单只解决"多了什么"，不解决"少了什么"：Backend 少给必填字段时，
    Pydantic 会抛 ``ValidationError``。这是**契约被破坏**，不是可预期的业务结果，
    因此不在这里吞掉 —— 由 P8-2 决定如何翻译成 State 里的失败。
    """
    selected = {key: value for key, value in payload.items() if key in AgentRule.model_fields}
    return AgentRule.model_validate(selected)


__all__ = [
    "AgentRule",
    "EvaluationFailureReason",
    "RuleEvaluationResult",
    "RuleEvaluationStatus",
    "RuleRisk",
    "RuleSetSnapshot",
    "rule_from_backend",
]
