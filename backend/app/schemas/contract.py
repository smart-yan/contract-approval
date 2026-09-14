"""合同接入相关的 Pydantic DTO（架构文档 §8）。

**响应模型不是 ORM 对象**：API 直接返回 ORM 实例会把内部字段（乃至将来
懒加载触发的关系）暴露出去。这里显式声明对外契约，也便于和前端 TS 类型对齐。

刻意**不返回**的内容：
* ``storage_path`` / ``storage_key`` —— 存储后端的内部标识
* 服务器绝对路径
* 任何凭据
"""

from __future__ import annotations

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


__all__ = ["ContractIngestResponse"]
