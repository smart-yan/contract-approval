"""合同接入相关的 Pydantic DTO（架构文档 §8）。

**响应模型不是 ORM 对象**：API 直接返回 ORM 实例会把内部字段（乃至将来
懒加载触发的关系）暴露出去。这里显式声明对外契约，也便于和前端 TS 类型对齐。

刻意**不返回**的内容：
* ``storage_path`` / ``storage_key`` —— 存储后端的内部标识
* 服务器绝对路径
* 任何凭据
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ContractIngestResponse(BaseModel):
    """上传接入结果（P4）。"""

    # ---- 合同 ----
    contract_id: int = Field(description="合同 ID")
    contract_no: str = Field(description="合同编号（由客户端提供）")
    title: str = Field(description="合同名称")
    contract_type: str = Field(description="合同类型，取值见 constants.ContractType")
    contract_status: str = Field(description="合同状态（派生自当前任务状态）")

    # ---- 附件 ----
    file_id: int = Field(description="附件 ID")
    filename: str = Field(description="原始文件名（仅作为展示用 metadata）")
    file_size: int = Field(description="文件字节数")
    file_type: str = Field(description="服务端识别出的文件类型：DOCX / PDF / JPEG / PNG / TIFF / BMP")
    sha256: str = Field(description="文件内容 SHA-256（附件去重依据）")
    parse_status: str = Field(description="解析状态；P4 只会是 PENDING")

    # ---- 审查任务 ----
    review_task_id: int = Field(description="审查任务 ID")
    task_status: str = Field(description="任务状态，取值见 constants.TaskStatus")
    task_stage: str = Field(description="任务阶段，取值见 constants.TaskStage")

    # ---- 幂等（两层，互相独立）----
    reused: bool = Field(
        description="**文件层**幂等：true 表示该 sha256 已存在，复用了已有的 Contract 与 ContractFile"
    )
    task_reused: bool = Field(
        description="**任务层**幂等：true 表示同一个 Contract + 同一个 File + 同一套审查配置"
        "已经存在 ReviewTask，直接复用了它。为 false 且 reused 为 true 时，"
        "表示复用了文件、但因审查配置不同而新建了一个 ReviewTask"
    )


# =========================================================================== #
# 合同查询（P11-3）
# =========================================================================== #
class ReviewTaskSummary(BaseModel):
    """列表项里的**最新任务摘要**。

    ⚠️ 为什么列表状态不能取 ``Contract.status`` / ``Contract.current_task_id``：
    那两个字段在 P4 建合同时写下后就**没人再维护**（``status`` 恒为建库时的值、
    ``current_task_id`` 恒为 ``NULL``），拿它们当状态来源会显示一个陈旧的事实。
    真正的进度信号在 ``review_task.current_stage`` —— 它由文档层与风险层真实推进。
    """

    task_id: int = Field(description="审查任务 ID（点进工作台要用它）")
    status: str = Field(description="任务状态，取值见 constants.TaskStatus")
    current_stage: str = Field(
        description="阶段级断点标记，取值见 constants.TaskStage。"
        "**这才是当前可用的进度信号**（``status`` 目前恒为 pending）"
    )
    progress: int = Field(
        ge=0,
        le=100,
        description="进度百分比。⚠️ 由 ``current_stage`` **推导**，不是数据库里那一列 —— "
        "``review_task.progress`` 从来没有被更新过（恒为 0）。映射见 services/contract_query.py",
    )


class ContractListItem(BaseModel):
    """合同列表的一项。

    ``latest_task`` 为 ``None`` 表示**这个合同还没有任何审查任务** ——
    不伪造一个默认任务，前端据此显示"尚未发起审查"。
    """

    contract_id: int = Field(description="合同 ID")
    contract_no: str = Field(description="合同编号")
    title: str = Field(description="合同名称")
    contract_type: str = Field(description="合同类型，取值见 constants.ContractType")
    created_at: datetime = Field(
        description="入库时刻。⚠️ **naive UTC**（项目统一存 UTC，不带时区偏移）—— "
        "前端展示时需要自行按本地时区换算，不要直接当本地时间用"
    )
    latest_task: ReviewTaskSummary | None = Field(
        default=None, description="该合同下 id 最大的审查任务；没有任务时为 null"
    )


__all__ = ["ContractIngestResponse", "ContractListItem", "ReviewTaskSummary"]
