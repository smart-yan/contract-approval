"""回写域模型：``writeback_record``（架构文档 §7.2、§12）。

设计说明
--------
* ``idempotency_key`` 为 ``NOT NULL UNIQUE``：回写最危险的场景不是失败，
  而是"**写成功了但响应超时**"导致重复评论。幂等键必须非空，否则 UNIQUE
  约束会被多个 NULL 静默绕过。
* ``content_md`` 是回写内容的**快照**，作为审计证据保留 —— 即使后续风险被修改，
  也能还原当时写出去的原文。
* §7.1 提到"历史在 ``writeback_log``"，但该表在 §7.1 的表清单与 §7.2 中
  **均无字段定义**，经架构裁决 P3 不创建，留待 P12。

回写业务逻辑（Markdown 生成、审批系统调用、重试）属于 **P12**，本模块只建表。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseMixin


class WritebackRecord(Base, BaseMixin):
    """审批意见回写记录（§7.2 writeback_record）。"""

    __tablename__ = "writeback_record"

    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_task.id"), nullable=False, comment="所属审查任务"
    )
    contract_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contract.id"), nullable=False, comment="所属合同"
    )
    approval_instance_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("approval_instance.id"),
        nullable=False,
        comment="目标审批单。建真实 FK：回写必然针对某个审批单",
    )

    content_md: Mapped[str] = mapped_column(
        Text, nullable=False, comment="回写内容快照（Markdown），审计证据"
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="content_md 的 SHA-256，参与幂等键计算"
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="回写状态，取值见 constants.WritebackStatus（§6.2）。初始为 not_written",
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已尝试次数")

    idempotency_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="sha256(task_id + content_hash)；NOT NULL UNIQUE，防止重复评论",
    )

    external_comment_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="审批系统返回的评论 ID"
    )

    request_body: Mapped[str | None] = mapped_column(Text, nullable=True, comment="请求体（脱敏）")
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True, comment="响应体（脱敏）")
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")

    operator_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="操作人 sys_user.id（sys_user 属 P4，暂不建 FK）"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=3), nullable=True, comment="开始写回时刻"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=3), nullable=True, comment="写回结束时刻"
    )
