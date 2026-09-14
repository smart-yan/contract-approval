"""Mock 审批域模型：``approval_instance`` / ``approval_comment``（架构文档 §7.2、§14）。

定位
----
这不是"假数据填充"，而是**防腐层（Anti-Corruption Layer）**：
它扮演外部审批系统的角色，有独立的数据表，通过 ``ApprovalClient`` 抽象接口访问。
将来对接真实系统（钉钉/飞书/OA）时只替换适配器，业务代码零改动。

设计说明
--------
* ``approval_comment.external_id`` **P3 不加 UNIQUE**（架构裁决 O5）：
  §7.2 虽写"UNIQUE"，但"全局唯一还是审批单内唯一"尚未定义，P3 不猜。
  迁移到真实系统（P14）时再确定。
* ``comment_type`` / ``source`` / ``status`` 一律用 ``VARCHAR``，**不新增枚举**：
  §7.2 给出了前两者的取值（USER/SYSTEM_REVIEW、MANUAL/CONTRACT_REVIEW_SYSTEM），
  但新增枚举需另行架构裁决；``status`` 的取值 §7.2 完全未定义，P3 不猜。

Mock 审批的业务逻辑（拉取待办、评论写回、故障注入）属于 **P14**，本模块只建表。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseMixin


class ApprovalInstance(Base, BaseMixin):
    """Mock 审批单（§7.2 approval_instance）。"""

    __tablename__ = "approval_instance"

    instance_no: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="审批单号。⚠️ 唯一性语义未定义，P3 不加 UNIQUE（裁决 O4）"
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="审批单标题")
    applicant: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="申请人姓名。Mock 域用纯字符串，不关联 sys_user"
    )
    dept: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="申请部门")
    amount: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True, comment="金额（禁止 float）"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="审批单状态。取值由 P14 定义，P3 不猜"
    )

    contract_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("contract.id"),
        nullable=True,
        comment="关联合同。§7.1 关系为 0:1，故可空",
    )


class ApprovalComment(Base, BaseMixin):
    """Mock 审批单评论（§7.2 approval_comment）。回写模块的目标表。"""

    __tablename__ = "approval_comment"

    instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("approval_instance.id"), nullable=False, comment="所属审批单"
    )

    author: Mapped[str] = mapped_column(String(64), nullable=False, comment="评论人姓名")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="评论内容（回写时为 Markdown）")

    comment_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="评论类型：USER / SYSTEM_REVIEW（§7.2）"
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="来源：MANUAL / CONTRACT_REVIEW_SYSTEM（§7.2）。回写产生的评论后者",
    )

    external_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="外部系统评论 ID。⚠️ P3 不加 UNIQUE（裁决 O5）：全局唯一还是审批单内唯一尚未定义",
    )
