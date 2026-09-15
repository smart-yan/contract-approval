"""文档层持久化的对外契约（P10-1）。

它解决什么
---------
Agent 解析出 ``document_block``、切分出 ``clause``、抽取出 ``contract_metadata``，
但**没有任何通道把它们写进库**。本契约是那条通道。

为什么是**一次请求**
------------------
三张表之间有**硬性数据依赖**：

::

    contract_metadata.source_block_id  ─┐
    clause.start_block_id / end_block_id ┴─→ document_block.id

而这些 id **在本请求处理过程中才产生**。拆成三个接口，就会出现
"clause 指向一个还不存在的 block" 或者 "block 落库了、clause 没落" 的中间态 ——
库里留下一份**结构断裂**的文档。因此它们必须同属一个事务。

块之间怎么互相引用
----------------
既然 block 的数据库主键要等 INSERT 之后才有，请求里就**只能用位置引用**：
``ClauseCreate.start_block_index`` / ``end_block_index`` 与
``MetadataItemCreate.source_block_index`` 都是 **``blocks[]`` 数组里的下标**
（从 0 起），由服务端解析成真实的 ``document_block.id``。

⚠️ 这**不是**在新增坐标语义：它只是"本次请求里的第几块"，
服务端只做**边界校验**（下标越界即拒绝），不理解文档结构、不计算任何坐标。

服务端**不做**的事（边界）
------------------------
不解 DOCX / PDF、不计算段落坐标、不重新切分条款、不重新抽取元数据。
它只做三件事：**校验、事务、持久化**。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.core.constants import BlockType, ClauseType, ExtractMethod


class DocumentBlockCreate(BaseModel):
    """一个文档块（Agent 的 ``Paragraph`` 在边界上的形状 + Backend 需要的定位列）。"""

    order_index: int = Field(ge=0, description="全文档线性顺序，前端渲染顺序")
    paragraph_index: int = Field(ge=0, description="段落索引（DOCX 为真实段号）")
    block_type: BlockType = Field(description="块类型，取值见 constants.BlockType")
    text: str = Field(description="归一化后的文本")

    char_start_global: int = Field(ge=0, description="全文档全局起始偏移。**由 Agent 计算**，服务端只存不算")
    char_end_global: int = Field(ge=0, description="全文档全局结束偏移，区间语义 [start, end)")

    raw_text: str | None = Field(
        default=None,
        description="⚠️ **当前没有真实来源**：P6 的 DocxParser 在解析时就丢弃了归一化前的原文"
        "（只保留 ``normalize_text`` 的结果），因此本阶段填的就是 ``text``。"
        "**它不代表真实的未归一化原文**，等 Parser 保留原文后才会名副其实。"
        "省略时服务端按 ``text`` 处理",
    )
    char_start_in_block: int | None = Field(
        default=None, description="块内起始偏移（归一化映射用）。省略时按 0 处理"
    )
    char_end_in_block: int | None = Field(
        default=None, description="块内结束偏移。省略时按 ``len(text)`` 处理"
    )

    # ---- 非 DOCX 才用得上，DOCX 一律为空（§10.4 禁止估算填充） ----
    page_number: int | None = Field(default=None, description="真实页码；DOCX 恒为 NULL（§10.4）")
    bbox_json: dict[str, Any] | None = Field(default=None, description="OCR/PDF 的坐标框")
    ocr_confidence: float | None = Field(default=None, description="OCR 置信度（仅扫描件有值）")

    @model_validator(mode="after")
    def _check_offsets(self) -> DocumentBlockCreate:
        """全局区间必须自洽 —— 一个 end < start 的区间会让前端高亮整段错位。"""
        if self.char_end_global < self.char_start_global:
            raise ValueError(
                f"char_end_global({self.char_end_global}) 小于 char_start_global({self.char_start_global})"
            )
        return self


class ClauseCreate(BaseModel):
    """一个条款（Agent 的 ``Clause`` 在边界上的形状）。"""

    clause_type: ClauseType = Field(description="条款类型，取值见 constants.ClauseType")
    text: str = Field(description="条款全文")
    extract_method: ExtractMethod = Field(description="切分方式，取值见 constants.ExtractMethod")

    clause_no: str | None = Field(default=None, max_length=64, description="条款号原文，如「第三条」")
    title: str | None = Field(default=None, max_length=255, description="条款标题")

    start_block_index: int = Field(
        ge=0, description="起始块在本次 ``blocks[]`` 里的**下标**（不是数据库主键 —— 主键此刻还不存在）"
    )
    end_block_index: int = Field(ge=0, description="结束块在本次 ``blocks[]`` 里的下标（闭区间）")

    char_start_global: int | None = Field(
        default=None, description="条款起始全局偏移。省略时取起始块的 ``char_start_global``"
    )
    char_end_global: int | None = Field(
        default=None, description="条款结束全局偏移。省略时取结束块的 ``char_end_global``"
    )

    page_start: int | None = Field(default=None, description="起始页码；DOCX 为 NULL（§10.4）")
    page_end: int | None = Field(default=None, description="结束页码")
    confidence: float | None = Field(default=None, description="切分置信度")


class MetadataItemCreate(BaseModel):
    """一条元数据提取项。"""

    field_key: str = Field(max_length=64, description="字段键，如 counterparty_name")
    field_label: str = Field(max_length=128, description="展示名")
    field_value: str = Field(description="字段值（已规范化）")
    value_type: str = Field(
        max_length=16,
        description="值类型。当前文档口径为 TEXT / AMOUNT / DATE / CODE —— "
        "⚠️ 项目里**没有**对应枚举（§7.2 只给了列注释），因此这里按自由文本接收，不设闭集校验",
    )
    extract_method: ExtractMethod = Field(description="提取方式，取值见 constants.ExtractMethod")

    source_block_index: int | None = Field(
        default=None, description="来源块在本次 ``blocks[]`` 里的下标；抽取失败时为空"
    )
    char_start_global: int | None = Field(default=None, description="定位起始偏移")
    char_end_global: int | None = Field(default=None, description="定位结束偏移")
    page_number: int | None = Field(default=None, description="真实页码；DOCX 为 NULL（§10.4）")
    confidence: float | None = Field(default=None, description="抽取置信度")


class DocumentPersistRequest(BaseModel):
    """一次"把这批文档层结果写入该任务"的请求。

    ``parse_status`` **显式传入**，不由服务端从"blocks 是否为空"推断：
    ``PARSED`` + 0 块（空文档 ``EMPTY``）与 ``FAILED`` 是**完全不同的两件事**，
    前者是"文档本身没内容"，后者是"我们没读出来"。让服务端去猜，
    就会把两者混为一谈。
    """

    parse_status: Literal["PARSED", "FAILED"] = Field(
        description="本次解析的结论。⚠️ 只接受这两个终态：``PENDING`` 是上传时的初始值，"
        "``PARSING`` 在同步编排里不可观测（Agent 在一次 HTTP 请求内跑完），都不该由调用方指定"
    )
    blocks: list[DocumentBlockCreate] = Field(
        default_factory=list, description="解析出的块。失败或空文档时为空列表"
    )
    clauses: list[ClauseCreate] = Field(default_factory=list, description="切分出的条款")
    metadata: list[MetadataItemCreate] = Field(default_factory=list, description="抽取出的元数据")

    @model_validator(mode="after")
    def _check_block_references(self) -> DocumentPersistRequest:
        """引用必须落在本次 ``blocks[]`` 范围内，且块序号不能重复。

        越界的引用是**调用方的错误**，不是"尽力而为"的场景 —— 放过去就会写出一条
        指向无关块（或压根没有块）的 clause/metadata。宁可整批拒绝。

        ``order_index`` 不允许重复：文件级复用要靠它在"本次请求的块"与
        "库里已有的块"之间建立一一对应，重复了就没有确定的映射。
        """
        seen: set[int] = set()
        for index, block in enumerate(self.blocks):
            if block.order_index in seen:
                raise ValueError(f"blocks[{index}] 的 order_index={block.order_index} 与前面的块重复")
            seen.add(block.order_index)

        total = len(self.blocks)
        for index, clause in enumerate(self.clauses):
            for label, position in (
                ("start_block_index", clause.start_block_index),
                ("end_block_index", clause.end_block_index),
            ):
                if position >= total:
                    raise ValueError(f"clauses[{index}].{label}={position} 越界（本次共 {total} 个块）")
            if clause.end_block_index < clause.start_block_index:
                raise ValueError(f"clauses[{index}] 的 end_block_index 小于 start_block_index")

        for index, item in enumerate(self.metadata):
            if item.source_block_index is not None and item.source_block_index >= total:
                raise ValueError(
                    f"metadata[{index}].source_block_index={item.source_block_index} "
                    f"越界（本次共 {total} 个块）"
                )
        return self


class DocumentPersistResponse(BaseModel):
    """持久化结果。"""

    task_id: int = Field(description="审查任务 ID")
    parse_status: str = Field(description="写入后的解析状态，取值见 constants.ParseStatus")
    blocks_created: int = Field(description="本次新建的块数。文件级复用时不新建")
    blocks_reused: int = Field(description="复用已有块的数量（同一 file 再次审查时）")
    clauses_persisted: int = Field(description="本次写入的条款数")
    metadata_persisted: int = Field(description="本次写入的元数据条数")
    current_stage: str = Field(description="更新后的任务阶段，取值见 constants.TaskStage")


__all__ = [
    "ClauseCreate",
    "DocumentBlockCreate",
    "DocumentPersistRequest",
    "DocumentPersistResponse",
    "MetadataItemCreate",
]
