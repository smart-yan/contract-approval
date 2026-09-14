"""合同域模型：``contract`` / ``contract_file``（架构文档 §7.2）。

设计说明
--------
* 金额一律 ``DECIMAL(18,2)``，**禁止 float**（§7.2 约定）。
* 日期三件套（签署/生效/到期）是**日历日**概念，用 ``DATE``，不做时区换算。
* ``applicant_id`` 与 ``approval_instance_id`` 按架构裁决**保留列但不建 FK**：
  前者目标表 ``sys_user`` 属 P4（P3 未创建），后者是为了避免与
  ``approval_instance.contract_id`` 形成循环外键（§7.2 也未标注它为 FK）。
* FK 一律不声明 ``ondelete`` / ``onupdate`` → 保持 MySQL 默认 RESTRICT，
  禁止级联删除摧毁审计证据（§6.1「历史任务全留存」）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import CHAR, BigInteger, Boolean, Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseMixin


class Contract(Base, BaseMixin):
    """合同主表（§7.2 contract）。"""

    __tablename__ = "contract"

    contract_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, comment="合同编号")
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="合同名称")
    contract_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="合同类型，取值见 constants.ContractType"
    )
    our_party: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="我方主体")
    counterparty: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="相对方主体")

    amount: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True, comment="合同金额（禁止 float）"
    )
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True, comment="币种，如 CNY")

    sign_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="签署日")
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="生效日")
    expire_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="到期日")

    dept: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="送审部门")

    applicant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="申请人 sys_user.id。⚠️ sys_user 属 P4，P3 暂不建 FK，P4 完成后通过增量迁移补",
    )

    source: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="合同来源，取值见 constants.ContractSource"
    )

    approval_instance_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="关联审批单 approval_instance.id。刻意不建 FK：§7.2 未标注，且可避免与 "
        "approval_instance.contract_id 形成循环外键",
    )

    current_task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="最近一次审查任务 review_task.id（冗余，避免列表页 join）。"
        "刻意不建 FK：与 review_task.contract_id 构成循环依赖，且 §7.2 已明确其为冗余字段",
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="派生字段：跟随 current_task 的状态，便于列表筛选。由 P5 建合同时连同任务一起写入",
    )


class ContractFile(Base, BaseMixin):
    """合同附件（§7.2 contract_file）。``sha256`` 是附件去重与幂等的基石。"""

    __tablename__ = "contract_file"

    contract_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contract.id"), nullable=False, comment="所属合同"
    )

    file_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="原始文件名")
    file_ext: Mapped[str] = mapped_column(String(16), nullable=False, comment="扩展名，如 docx")
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="字节数")

    sha256: Mapped[str] = mapped_column(
        CHAR(64),
        nullable=False,
        unique=True,
        comment="文件内容的 SHA-256 十六进制摘要。UNIQUE：同一文件重复上传时复用解析产物",
    )

    storage_path: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="相对 storage/uploads/{contract_id}/ 的路径"
    )

    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="页数")
    is_scanned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否判定为扫描件（需走 OCR）"
    )
    parse_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="单文件解析状态。具体取值由 P7 解析阶段确定"
    )
