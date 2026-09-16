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
* ``approval_comment.idempotency_key`` 是 **P15-3a** 新增的列（见下）。
* ``comment_type`` / ``source`` / ``status`` 一律用 ``VARCHAR``，**不新增枚举**：
  §7.2 给出了前两者的取值（USER/SYSTEM_REVIEW、MANUAL/CONTRACT_REVIEW_SYSTEM），
  但新增枚举需另行架构裁决；``status`` 的取值 §7.2 完全未定义，P3 不猜。

Mock 审批的业务逻辑（拉取待办、评论写回、故障注入）属于 **P14**，本模块只建表。

### P15-3a：为什么补一列 ``idempotency_key``

§14.2 对评论写回的要求是"带 ``Idempotency-Key``；**重复键返回既有评论不新建**"，
§12 第 5 步还要求重试前"先查审批系统是否已有该幂等键的评论"。而 P3 建表时
**没有任何列能承载这个键**，于是那条要求一直没有落点。

架构裁决（P15-3a）明确否掉了两种"绕过去"的做法：

* ❌ 用 ``external_id = sha256(key)`` 这种**派生 ID** 冒充幂等 —— 那是把
  "外部系统给自己评论起的标识"和"调用方给的请求身份"混成一个东西；
* ❌ 用 ``(instance_id, content)`` 判重 —— 那是把幂等键**等同于评论内容**，
  内容相同但请求不同（例如两个任务写出同一段文字）会被错误地判成重复。

因此补一列，并按"**请求身份在审批单内唯一**"建约束：
``UNIQUE(instance_id, idempotency_key)``。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Numeric, String, Text, UniqueConstraint
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
    """Mock 审批单评论（§7.2 approval_comment）。回写模块的目标表。

    ⚠️ ``idempotency_key`` 是 **P15-3a** 新增的列（见本文件顶部的说明）：
    §14.2 要求评论写回"带 ``Idempotency-Key``；重复键返回既有评论不新建"，
    而 P3 建表时**没有任何列能承载这个键** —— 于是那条要求一直无处落地。
    """

    __tablename__ = "approval_comment"

    __table_args__ = (
        # 同一审批单内，同一个 Idempotency-Key 只能有一条评论。
        #
        # ⚠️ **复合唯一，不是列级 unique**：幂等键是"**外部请求**的身份"，
        # 不是评论的全局标识 —— 两个审批单各自收到同一个键（例如同一份意见
        # 被回写到两个审批单）并不冲突。
        # ⚠️ **列可空**：人工评论（``source=MANUAL``）没有"外部请求身份"这件事，
        # 硬要它编一个键就是伪造。MySQL 的唯一索引**不把多个 NULL 视为冲突**，
        # 因此人工评论不受这条约束影响 —— 这个语义是刻意依赖的，不是巧合。
        UniqueConstraint("instance_id", "idempotency_key"),
    )

    instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("approval_instance.id"), nullable=False, comment="所属审批单"
    )

    author: Mapped[str] = mapped_column(String(64), nullable=False, comment="评论人姓名")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="评论内容（回写时为 Markdown）")

    comment_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="评论类型：USER / SYSTEM_REVIEW（§7.2）"
    )
    source: Mapped[str] = mapped_column(
        # ⚠️ 32 而不是 16：§7.2 的取值 ``CONTRACT_REVIEW_SYSTEM`` 有 **22** 个字符，
        # P3 建的 16 位列**存不下它**（MySQL 1406）。P15-3b 的回写链路必然要写这个值，
        # 因此架构裁决把列加宽到 32（见 migration 181445d74bd3）。
        # 8 位余量是给将来的 source 取值留的，不必为几个字符再做一次迁移。
        String(32),
        nullable=False,
        comment="来源：MANUAL / CONTRACT_REVIEW_SYSTEM（§7.2）。回写产生的评论后者",
    )

    external_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="外部系统评论 ID。⚠️ P3 不加 UNIQUE（裁决 O5）：全局唯一还是审批单内唯一尚未定义",
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="回写请求的幂等键（外部系统的 Idempotency-Key）。"
        "⚠️ 可空：人工评论没有外部请求身份。UNIQUE(instance_id, idempotency_key)",
    )
