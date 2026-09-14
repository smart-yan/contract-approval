"""规则域模型：``review_rule_set`` / ``review_rule``（架构文档 §7.2、§11.1）。

⚠️ 字段来源说明
--------------
§7.2 的小节标题是「review_rule_set / review_rule 规则」，但字段表**只定义了
``review_rule``，``review_rule_set`` 一个字段都没有**。``review_rule_set`` 的字段
由架构裁决明确给出（见下），**不得自行扩展**。

规则求值引擎（``RuleEngine`` / evaluators / scorer）属于 **P9**，本模块只建表。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, BaseMixin


class ReviewRuleSet(Base, BaseMixin):
    """审查规则集（字段由架构裁决给出，非 §7.2 原文）。

    ``version`` 是 ``review_task.idempotency_key`` 的组成项
    （``sha256(contract_id + file_sha256 + rule_set_version + prompt_version)``），
    因此该列不可为空。
    """

    __tablename__ = "review_rule_set"

    name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="规则集名称，如「采购合同审查清单 v1」"
    )
    contract_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="适用的合同类型，取值见 constants.ContractType"
    )
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="说明")
    version: Mapped[str] = mapped_column(String(32), nullable=False, comment="规则集版本，参与任务幂等键计算")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="创建者 sys_user.id（sys_user 属 P4，暂不建 FK）"
    )


class ReviewRule(Base, BaseMixin):
    """审查规则（§7.2 review_rule）。"""

    __tablename__ = "review_rule"

    rule_set_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review_rule_set.id"),
        nullable=False,
        comment="所属规则集。§7.2 字段表未列出此列，但「rule_code UNIQUE within set」"
        "与 §7.1 的 rule_set 1:N rule 关系都要求它存在（经架构裁决补充）",
    )

    rule_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="规则编码，如 LIAB_UNEQUAL_001；在所属规则集内唯一"
    )
    rule_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="规则名称")
    dimension: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="审查维度，如「违约责任」「知识产权」（取值由 P9 定义）"
    )
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="规则说明")

    rule_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="求值器类型，取值见 constants.RuleType"
    )
    expression: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        comment='求值表达式，如 {"keywords":["无限责任"],"logic":"ANY"} 或 '
        '{"field":"amount","op":"gt","value":1000000}',
    )
    target_clause_types: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="限定作用范围：条款类型数组，空表示不限"
    )

    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="命中后的风险等级，取值见 constants.RiskLevel"
    )
    legal_basis: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="法律依据，如《民法典》第 585 条"
    )
    suggestion_template: Mapped[str | None] = mapped_column(Text, nullable=True, comment="默认修改建议模板")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="展示与执行顺序")

    __table_args__ = (
        # §7.2「rule_code UNIQUE within set」的落地：复合唯一，而非全局唯一
        UniqueConstraint("rule_set_id", "rule_code"),
    )
