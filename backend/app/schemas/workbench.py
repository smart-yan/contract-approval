"""审查工作台的对外契约（P11-4）。

它是什么
-------
**一个审查任务的全部展示数据**，一次性返回给前端工作台：

::

    ReviewTaskWorkbenchResponse
    ├── task      本次审查（状态 / 阶段 / 进度 / 结论）
    ├── contract  合同主数据
    ├── file      被审查的附件
    ├── blocks[]  原文段落（**文件级**）
    ├── clauses[] 条款
    ├── metadata[] 元数据
    └── risks[]   风险

为什么是**一个聚合接口**
----------------------
工作台首屏要渲染的东西就是这些，而且**风险高亮必须同时拿到 ``blocks`` 与 ``risks``**
（点风险卡片时原文可能还没到，前端就得写一个等待状态机）。分成 5 个接口除了多 4 次
往返，还会在"两次请求之间有人重跑了审查"时把 A 次的 risks 与 B 次的 clauses 混起来 ——
而两者的坐标语义不同（clauses 任务级、blocks 文件级），混起来会把风险高亮到错误段落，
**且不会报错**。一次查询、一个快照，这类竞态在结构上不存在。

DTO 与 ORM 的边界
----------------
这里是**显式的响应契约**，不是 ORM 的转储：字段逐个声明，内部列（存储路径、
申请人工号、幂等键……）一律不出现。前端只依赖本模块，不依赖数据库结构。

两处**派生字段**（数据库里没有、由本层算出来）
------------------------------------------
* ``WorkbenchClause.start_paragraph_index`` / ``end_paragraph_index``
  ← ``start_block_id`` / ``end_block_id`` 经 ``DocumentBlock.id → paragraph_index`` 换算
* ``WorkbenchMetadataItem.source_paragraph_index`` ← 同上

为什么换算放在 Backend 而不是前端：``start_block_id → paragraph_index`` 是一条
**数据库结构知识**，抄到前端就等于把同一份坐标系实现两遍，schema 改了会两边漂移
且不报错。``start_block_id`` / ``end_block_id`` 本身仍然保留在 DTO 里（可追溯、调试用），
只是前端**不需要**消费它们。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class WorkbenchTask(BaseModel):
    """本次审查任务。"""

    task_id: int = Field(description="审查任务 ID")
    status: str = Field(description="任务状态，取值见 constants.TaskStatus")
    current_stage: str = Field(
        description="阶段级断点标记，取值见 constants.TaskStage。"
        "**当前可用的进度信号**（``status`` 目前恒为 pending）"
    )
    progress: int = Field(
        ge=0,
        le=100,
        description="进度百分比。⚠️ 由 ``current_stage`` **推导**（复用 P11-3 的映射），"
        "不是数据库里那一列 —— ``review_task.progress`` 从来没有被更新过",
    )
    created_at: datetime = Field(description="入库时刻（naive UTC）")
    finished_at: datetime | None = Field(
        default=None,
        description="结束时刻。⚠️ **当前恒为 NULL**：P10 的裁决是只推进 ``current_stage``、"
        "不动状态机，因此任务还没被标记为结束",
    )
    risk_level_final: str | None = Field(
        default=None, description="综合风险等级（§11.2）。⚠️ 当前恒为 NULL —— 评分器尚未实现"
    )
    conclusion: str | None = Field(
        default=None, description="审查结论（§11.2）。⚠️ 当前恒为 NULL —— 评分器尚未实现"
    )


class WorkbenchContract(BaseModel):
    """合同主数据。"""

    contract_id: int = Field(description="合同 ID")
    contract_no: str = Field(description="合同编号")
    title: str = Field(description="合同名称")
    contract_type: str = Field(description="合同类型，取值见 constants.ContractType")
    our_party: str | None = Field(default=None, description="我方主体")
    counterparty: str | None = Field(default=None, description="相对方主体")
    amount: Decimal | None = Field(
        default=None, description='合同金额。⚠️ 以**字符串**序列化（如 ``"1234.50"``）—— Decimal 的默认口径'
    )
    currency: str | None = Field(default=None, description="币种，如 CNY")
    sign_date: date | None = Field(default=None, description="签署日")
    effective_date: date | None = Field(default=None, description="生效日")
    expire_date: date | None = Field(default=None, description="到期日")
    dept: str | None = Field(default=None, description="送审部门")


class WorkbenchFile(BaseModel):
    """被审查的附件。

    ⚠️ ``file_type`` 是**派生**的：``contract_file`` 表只存 ``file_ext``（如 ``.docx``），
    没有类型码列。这里复用上传时用的同一个函数（``type_code_for_extension``）还原，
    保证与 ``POST /contracts`` 的响应口径一致。
    """

    file_id: int = Field(description="附件 ID")
    file_name: str = Field(description="原始文件名")
    file_type: str | None = Field(default=None, description="文件类型码：DOCX / PDF / JPEG / …")
    sha256: str = Field(description="文件内容 SHA-256")
    parse_status: str = Field(description="解析状态，取值见 constants.ParseStatus")


class WorkbenchBlock(BaseModel):
    """一个原文段落块。

    ⚠️ ``blocks`` 是**文件级**数据（``document_block`` 有 ``file_id``、没有 ``task_id``）：
    同一个文件跑了两个任务时，它们看到的是**同一批段落**。条款/元数据/风险才是任务级的。
    """

    block_id: int = Field(description="块 ID")
    order_index: int = Field(description="全文档线性顺序 —— 前端按它渲染原文")
    paragraph_index: int = Field(description="段落序号（P6-2 的位置契约，风险定位靠它）")
    block_type: str = Field(description="块类型，取值见 constants.BlockType")
    text: str = Field(description="段落文本")
    char_start_global: int = Field(description="全文档全局起始偏移")
    char_end_global: int = Field(description="全文档全局结束偏移，区间语义 [start, end)")


class WorkbenchClause(BaseModel):
    """一个条款。

    ``start_paragraph_index`` / ``end_paragraph_index`` 是**换算出来的**（见模块 docstring）：
    它们才是前端要的坐标。``start_block_id`` / ``end_block_id`` 保留原值供追溯。

    区间无法换算时（块的 id 为空、或不在本文件的块集合里）为 ``None`` ——
    **不伪造一个段落号**：宁可前端少显示一个范围，也不要让它去高亮错误的段落。
    """

    clause_id: int = Field(description="条款 ID")
    clause_no: str | None = Field(default=None, description="条款号原文，如「第三条」")
    clause_type: str = Field(description="条款类型，取值见 constants.ClauseType")
    title: str | None = Field(default=None, description="条款标题")
    text: str = Field(description="条款全文")

    start_paragraph_index: int | None = Field(default=None, description="起始段落序号（由起始块换算）")
    end_paragraph_index: int | None = Field(default=None, description="结束段落序号（由结束块换算）")

    start_block_id: int | None = Field(default=None, description="起始块 ID（原值，供追溯）")
    end_block_id: int | None = Field(default=None, description="结束块 ID（原值，供追溯）")


class WorkbenchMetadataItem(BaseModel):
    """一条元数据提取项。

    ⚠️ **没有 quote**：``contract_metadata`` 表里没有这一列（P10 已确认），
    定位由 ``source_block_id`` / ``source_paragraph_index`` 表达。
    """

    field_key: str = Field(description="字段键，如 counterparty_name")
    field_label: str = Field(description="展示名")
    field_value: str = Field(description="字段值（已规范化）")
    value_type: str = Field(description="值类型。⚠️ 项目里没有对应枚举，按自由文本透传")
    extract_method: str = Field(description="提取方式，取值见 constants.ExtractMethod")
    source_block_id: int | None = Field(default=None, description="来源块 ID（原值）")
    source_paragraph_index: int | None = Field(
        default=None, description="来源段落序号（由来源块换算）；没有来源块时为 null"
    )


class WorkbenchRisk(BaseModel):
    """一条风险。

    ``paragraph_index`` 是 P10 冻结的定位坐标 —— 前端拿它去 ``blocks`` 里找同号的
    ``paragraph_index``，滚动并高亮那一段。**不需要任何定位算法**。
    """

    risk_id: int = Field(description="风险 ID")
    risk_code: str | None = Field(default=None, description="规则编码；纯 LLM 风险为 null")
    risk_title: str = Field(description="风险标题")
    dimension: str = Field(description="审查维度")
    risk_level: str = Field(description="风险等级，取值见 constants.RiskLevel")
    source: str = Field(description="风险来源，取值见 constants.RiskSource")
    reason: str | None = Field(default=None, description="风险成因")
    legal_basis: str | None = Field(default=None, description="法律依据")
    original_text: str | None = Field(
        default=None,
        description="命中的原文片段。⚠️ 与请求侧的 AgentRiskItem.original_text 语义不同（那是段落原文）",
    )
    paragraph_index: int | None = Field(
        default=None, description="命中段落序号 —— **前端定位用这个**。字段可空，但 P10 写入时必填"
    )
    clause_id: int | None = Field(default=None, description="命中的条款 ID；纯 LLM 风险可能为空")
    locator_type: str = Field(description="定位方式，取值见 constants.LocatorType")
    review_status: str = Field(description="人工复核状态，取值见 constants.RiskReviewStatus")


class ReviewTaskWorkbenchResponse(BaseModel):
    """工作台的全部数据。

    各集合**可以为空**（解析失败、空文档、还没跑风险审查……），那是**正常的结论**，
    接口照样返回 200 —— 只有 ``task_id`` 不存在才是 404。
    """

    task: WorkbenchTask = Field(description="本次审查任务")
    contract: WorkbenchContract = Field(description="合同主数据")
    file: WorkbenchFile = Field(description="被审查的附件")
    blocks: list[WorkbenchBlock] = Field(default_factory=list, description="原文段落，按 order_index 升序")
    clauses: list[WorkbenchClause] = Field(default_factory=list, description="条款，按 id 升序")
    metadata: list[WorkbenchMetadataItem] = Field(default_factory=list, description="元数据，按 id 升序")
    risks: list[WorkbenchRisk] = Field(default_factory=list, description="风险，按 id 升序")


__all__ = [
    "ReviewTaskWorkbenchResponse",
    "WorkbenchBlock",
    "WorkbenchClause",
    "WorkbenchContract",
    "WorkbenchFile",
    "WorkbenchMetadataItem",
    "WorkbenchRisk",
    "WorkbenchTask",
]
