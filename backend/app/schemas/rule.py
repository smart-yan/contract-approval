"""规则读取的对外契约（P8-0）。

**只读**：这里定义的形状只用于把规则**取出来**给 Agent，
不涉及规则的创建/修改（那属于后续的规则管理阶段）。

字段口径原则
-----------
``expression`` / ``target_clause_types`` / ``dimension`` **按数据库现有值原样返回**，
不在这一层做任何归一化、枚举化或结构重构 —— 它们是业务可配置的数据，
Backend 只负责搬运，解释权在求值引擎（Agent 侧）。

⚠️ 这里返回的是 Backend 自己的资源标识（``id``），与 P4 的
``ContractIngestResponse`` 暴露 ``contract_id`` 是同一口径。
Agent 侧是否把它带进自己的契约，是 Agent 的决定。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RuleSetSummary(BaseModel):
    """规则集摘要。"""

    id: int = Field(description="规则集 ID")
    name: str = Field(description="规则集名称，如「采购合同审查清单 v1」")
    contract_type: str = Field(description="适用的合同类型，取值见 constants.ContractType")
    version: str = Field(
        description="规则集版本。它参与 review_task.idempotency_key 的计算，因此调用方应当把它一并记录下来"
    )
    description: str | None = Field(default=None, description="说明")


class RuleItem(BaseModel):
    """一条规则的完整定义。"""

    id: int = Field(description="规则 ID")
    rule_code: str = Field(description="规则编码，如 LIAB_UNLIMITED_001；在所属规则集内唯一")
    rule_name: str = Field(description="规则名称")
    dimension: str = Field(
        description="审查维度。**原样返回数据库里的值** —— 当前 seed 用的是中文展示名"
        "（如「知识产权」），这一层不把它映射成枚举码"
    )
    description: str | None = Field(default=None, description="规则说明")

    rule_type: str = Field(description="求值器类型，取值见 constants.RuleType")
    expression: dict[str, Any] = Field(
        description="求值表达式。**原样返回 JSON 契约**，形状由 rule_type 决定："
        'KEYWORD → {"keywords": [...], "logic": "ANY"}；'
        'THRESHOLD → {"field": ..., "op": ..., "value": ...}'
    )
    target_clause_types: list[str] | None = Field(
        default=None,
        description="限定作用范围：条款类型数组；**为空表示不限**（全文档生效）",
    )

    severity: str = Field(description="命中后的风险等级，取值见 constants.RiskLevel")
    legal_basis: str | None = Field(default=None, description="法律依据")
    suggestion_template: str | None = Field(default=None, description="默认修改建议模板")
    sort_order: int = Field(description="展示与执行顺序；列表已按它排好")


class EffectiveRuleSetResponse(BaseModel):
    """某合同类型下**当前启用**的规则集及其启用规则。

    ``rule_set`` 为 ``null`` 表示该合同类型下没有启用的规则集 ——
    **这不是错误**（架构裁决：没有规则集不能阻止上传），因此仍返回 200，
    ``rules`` 为空数组。
    """

    contract_type: str = Field(description="查询的合同类型")
    rule_set: RuleSetSummary | None = Field(default=None, description="当前启用的规则集；没有则为 null")
    rules: list[RuleItem] = Field(
        default_factory=list, description="该规则集下的启用规则，按 sort_order 升序"
    )


__all__ = ["EffectiveRuleSetResponse", "RuleItem", "RuleSetSummary"]
