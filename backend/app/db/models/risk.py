"""风险域模型：``risk_item`` / ``risk_suggestion``（架构文档 §7.2、§9.2、§10.5）。

设计说明
--------
* ``clause_id`` 为 **nullable**：§11.1 的 ``MISSING``（必备条款缺失）类规则
  **没有对应的条款**，非空会导致这类风险无法入库（经架构裁决确认）。
* ``char_start_global / char_end_global`` 为 **nullable**：§10.5 的 L4
  定位降级（``CLAUSE_FALLBACK``）只到条款级、不产生精确区间。
* ``risk_item.confidence`` 是"风险判断本身"的置信度，
  与 ``anchor_score``（定位置信度）是**两回事**，不要混淆。
* ``standard_clause_id`` 按架构裁决**保留列但不建 FK**：目标表属 P13。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, BaseMixin


class RiskItem(Base, BaseMixin):
    """风险项（§7.2 risk_item）—— 审查结论的最小单元。"""

    __tablename__ = "risk_item"

    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_task.id"), nullable=False, comment="所属审查任务"
    )
    contract_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contract.id"), nullable=False, comment="所属合同"
    )
    clause_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("clause.id"),
        nullable=True,
        comment="命中的条款。⚠️ 可为空：MISSING 类（必备条款缺失）风险没有对应条款",
    )
    rule_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("review_rule.id"),
        nullable=True,
        comment="来源规则；纯 LLM 风险为空",
    )

    risk_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="风险编码；规则来源时取 rule_code，LLM 来源可能为空"
    )
    risk_title: Mapped[str] = mapped_column(String(255), nullable=False, comment="风险项名称")
    dimension: Mapped[str] = mapped_column(String(32), nullable=False, comment="审查维度")

    risk_level: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="风险等级，取值见 constants.RiskLevel"
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="风险来源，取值见 constants.RiskSource"
    )

    reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="风险成因分析")
    legal_basis: Mapped[str | None] = mapped_column(Text, nullable=True, comment="法律合规依据")

    original_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="命中的原文片段（LLM quote 反查结果）"
    )

    # ---------------------- 定位信息（前端高亮依据） ---------------------- #
    page_number: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="真实页码；DOCX 为 NULL（§10.4）"
    )
    paragraph_index: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="段落索引")
    char_start_global: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="⚠️ 可为空：CLAUSE_FALLBACK 降级时无精确区间（§10.5）"
    )
    char_end_global: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="全局结束偏移")
    locator_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="定位方式，取值见 constants.LocatorType；与所属附件一致"
    )

    anchor_method: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="坐标反查方式，取值见 constants.AnchorMethod（§10.5 四级作用域）；规则来源为空",
    )
    anchor_score: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="定位置信度 0~1；< 0.8 时前端显示「定位待核对」角标"
    )
    occurrence_index: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="quote 在作用域内第几次出现（消歧结果，1-based）"
    )
    quote_context: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment='引文上下文 {"before": "...", "after": "..."}，用于消歧与复核'
    )

    confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="LLM 对该风险判断本身的置信度（≠ anchor_score）"
    )

    # ---------------------------- 人工复核 ---------------------------- #
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="复核状态，取值见 constants.RiskReviewStatus（§6.3）"
    )
    reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="复核人 sys_user.id（sys_user 属 P4，暂不建 FK）"
    )
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True, comment="复核意见")
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=3), nullable=True, comment="复核时刻")


class RiskSuggestion(Base, BaseMixin):
    """修改建议（§7.2 risk_suggestion）。"""

    __tablename__ = "risk_suggestion"

    risk_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("risk_item.id"), nullable=False, comment="所属风险项"
    )

    suggestion_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="建议类型，取值见 constants.SuggestionType"
    )
    original_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="原条款文本；ADD 类型（新增条款）时为空"
    )
    suggested_text: Mapped[str] = mapped_column(Text, nullable=False, comment="AI 生成的示范条款")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="修改理由")

    standard_clause_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="引用的标准条款库条目 standard_clause.id。"
        "⚠️ standard_clause 属 P13，P3 暂不建 FK，P13 完成后通过增量迁移补",
    )

    is_adopted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="法务是否采纳")
    adopted_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="采纳人 sys_user.id（sys_user 属 P4，暂不建 FK）"
    )
    adopted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=3), nullable=True, comment="采纳时刻")
    final_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="法务编辑后的最终文本。⚠️ 回写与报告使用 final_text，而不是 suggested_text（§7.2）",
    )
