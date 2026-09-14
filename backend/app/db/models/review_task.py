"""审查任务模型：``review_task``（架构文档 §7.2、§1.4、§6.1）。

这张表同时充当 **DB 队列**：worker 用 ``SELECT ... FOR UPDATE SKIP LOCKED``
按 ``(status, next_retry_at, priority, id)`` 抢占任务（§1.4）。

设计说明
--------
* ``version`` 是**普通 INTEGER**，刻意**不使用** SQLAlchemy 的 ``version_id_col``：
  §1.4 的抢占流程是手写 ``version = version + 1``，两者混用会互相打架。
* ``idempotency_key`` 为 ``NOT NULL UNIQUE``：MySQL 的 UNIQUE 允许多个 NULL，
  若允许为空则幂等约束会被**静默绕过**。
* 一个合同可多次审查，历史任务全留存（§6.1「completed 不可回退」）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import DATETIME, TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseMixin


class ReviewTask(Base, BaseMixin):
    """审查任务（§7.2 review_task）。"""

    __tablename__ = "review_task"

    contract_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("contract.id"),
        nullable=False,
        comment="所属合同。建真实 FK：它才是 contract↔task 关系的主体",
    )
    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contract_file.id"), nullable=False, comment="被审查的附件"
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="任务状态，取值见 constants.TaskStatus（§6.1）"
    )
    current_stage: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="阶段级断点标记，取值见 constants.TaskStage；断点续跑依据（§6.1）",
    )
    progress: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0, comment="0~100，前端进度条")
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="抢占排序用，值越大越先被抢"
    )

    worker_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="抢占它的 worker 标识（§1.4）"
    )
    locked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=3), nullable=True, comment="抢占时刻")
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=3), nullable=True, comment="心跳时间，自愈扫描依据（§13.5）"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=3), nullable=True, comment="退避重试的可见时间；NULL = 立即可抢"
    )

    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="乐观锁版本号，每次状态变更 +1（手工维护）"
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已重试次数")
    max_retry: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, comment="任务级重试上限（§13.2）"
    )

    block_reason_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="阻塞原因枚举，取值见 constants.BlockReasonCode（§6.1）"
    )
    block_reason_msg: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="人话原因，前端直接展示"
    )

    risk_level_final: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="综合风险等级，取值见 constants.RiskLevel；未完成时为空"
    )
    conclusion: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="审查结论，取值见 constants.ReviewConclusion；未完成时为空"
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="审查摘要（供报告与回写复用）")

    idempotency_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="sha256(contract_id + file_sha256 + rule_set_version + prompt_version)；"
        "NOT NULL UNIQUE —— 允许 NULL 会让幂等约束被静默绕过",
    )

    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因，排查用")
    started_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=3), nullable=True, comment="开始执行时刻"
    )
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=3), nullable=True, comment="结束时刻")

    __table_args__ = (
        # §7.2 指定的两个索引，名字与文档完全一致，故显式命名（不走 naming_convention）
        Index("idx_claim", "status", "next_retry_at", "priority", "id"),
        Index("idx_heartbeat", "status", "heartbeat_at"),
    )
