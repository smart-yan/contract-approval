"""文档解析域模型：``document_block`` / ``contract_metadata`` / ``clause``（架构文档 §7.2）。

``document_block`` 是**全项目坐标系的核心**（§10.2）：
``char_start_global / char_end_global`` 是全文档唯一的全局字符偏移区间，
风险、元数据、批注、建议全部复用它来定位原文，从根上避免多套坐标对不上。

设计说明
--------
* ``page_number`` **只存真实页码**：PDF / 扫描件 / 图片有值，**DOCX 恒为 NULL**
  （§10.4 明确禁止估算填充）。前端据 ``locator_type`` 显式选择定位文案，
  不得用 ``page_number is null`` 做隐式推断。
* ``ocr_confidence`` 用 ``FLOAT``：§7.2 明确如此；项目"禁 float"的约定针对**金额**。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, BaseMixin


class DocumentBlock(Base, BaseMixin):
    """文档段落块（§7.2 document_block）—— 原文坐标系的载体。"""

    __tablename__ = "document_block"

    contract_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contract.id"), nullable=False, comment="所属合同"
    )
    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contract_file.id"), nullable=False, comment="来源附件"
    )

    order_index: Mapped[int] = mapped_column(Integer, nullable=False, comment="全文档线性顺序，前端渲染顺序")
    block_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="块类型，取值见 constants.BlockType"
    )

    page_number: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="⚠️ 仅真实页码：PDF/扫描件/图片有值，DOCX 恒为 NULL，禁止估算填充（§10.4）",
    )
    paragraph_index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="段落索引：DOCX 为真实段号；PDF/图片为页内第 N 个文本块"
    )
    locator_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="定位方式，取值见 constants.LocatorType；解析时按文件类型一次性写入",
    )

    text: Mapped[str] = mapped_column(Text, nullable=False, comment="归一化后的文本")
    raw_text: Mapped[str] = mapped_column(
        Text, nullable=False, comment="原文（OCR 原始输出 / 未归一化），审计用"
    )

    char_start_in_block: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="块内起始偏移（归一化映射用）"
    )
    char_end_in_block: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="块内结束偏移（归一化映射用）"
    )
    char_start_global: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="全文档全局起始偏移 —— 所有定位的唯一依据"
    )
    char_end_global: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="全文档全局结束偏移，区间语义 [start, end)"
    )

    bbox_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="OCR/PDF 的坐标框，供渲染层精确框选"
    )
    ocr_confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="OCR 置信度（仅扫描件有值）"
    )

    __table_args__ = (
        # 名字交给 naming_convention 生成（§7.2 只规定索引列，未规定索引名）
        Index(None, "contract_id", "order_index"),
        Index(None, "contract_id", "char_start_global"),
    )


class ContractMetadata(Base, BaseMixin):
    """元数据提取项（§7.2 contract_metadata）—— 每一项都带原文定位。"""

    __tablename__ = "contract_metadata"

    contract_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contract.id"), nullable=False)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_task.id"), nullable=False, comment="产出它的审查任务"
    )

    field_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="字段键，如 counterparty_name / credit_code / contract_amount"
    )
    field_label: Mapped[str] = mapped_column(String(128), nullable=False, comment="展示名")
    field_value: Mapped[str] = mapped_column(Text, nullable=False, comment="字段值")
    value_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="值类型：TEXT / AMOUNT / DATE / CODE"
    )

    confidence: Mapped[float | None] = mapped_column(Float, nullable=True, comment="抽取置信度")

    source_block_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("document_block.id"), nullable=True, comment="来源块"
    )
    char_start_global: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="定位起始偏移（抽取失败时为空）"
    )
    char_end_global: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="定位结束偏移")
    page_number: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="真实页码；DOCX 为 NULL（§10.4）"
    )

    extract_method: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="提取方式，取值见 constants.ExtractMethod"
    )

    verified_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="人工校对者 sys_user.id（sys_user 属 P4，暂不建 FK）"
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=3), nullable=True, comment="人工校对时刻"
    )


class Clause(Base, BaseMixin):
    """条款（§7.2 clause）—— 由连续的 block 组成，是风险的挂载点。"""

    __tablename__ = "clause"

    contract_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("contract.id"), nullable=False)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_task.id"), nullable=False, comment="产出它的审查任务"
    )

    clause_no: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="条款号（如「第三条」）；识别不出时为空"
    )
    clause_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="条款类型，取值见 constants.ClauseType"
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="条款标题")

    start_block_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("document_block.id"), nullable=True, comment="起始块"
    )
    end_block_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("document_block.id"), nullable=True, comment="结束块"
    )

    char_start_global: Mapped[int] = mapped_column(Integer, nullable=False, comment="条款起始全局偏移")
    char_end_global: Mapped[int] = mapped_column(Integer, nullable=False, comment="条款结束全局偏移")

    page_start: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="起始页码；DOCX 为 NULL（§10.4）"
    )
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="结束页码")

    text: Mapped[str] = mapped_column(Text, nullable=False, comment="条款全文")

    extract_method: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="切分方式，取值见 constants.ExtractMethod"
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True, comment="切分置信度")
