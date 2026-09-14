"""Agent 编排入口的数据契约。

⚠️ 这不是 Backend 响应的副本
--------------------------
``ContractIngestResponse`` 有 15+ 个字段，其中大多数（合同名称、金额、日期……）
前端可以直接从 Backend 查询。这里只暴露 Agent 自己产出的东西：

* 工作流的结论（``workflow_status`` / ``validation_errors``）
* 解析阶段的产出（``parse_result``）
* 从 Backend 拿到的**标识**，用于让调用方接着去 Backend 查详情
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.document import ParseResult


class ReviewRunResponse(BaseModel):
    """一次合同审查编排的结果（Graph 结束时的状态投影）。"""

    workflow_status: Literal["completed", "rejected"] = Field(
        description="本工作流是否产出了**可用的文档**。"
        "completed = 上传通过且解析拿到了可用文档（PARSED 或 EMPTY）；"
        "rejected = 三种情况之一：被 validate 门禁拦下、解析失败、或解析根本没跑过。"
        "具体原因看 error_code —— status 只回答'有没有结果'，不回答'为什么没有'"
    )

    # ---- 来自 Backend 的标识（失败时可能为空）----
    contract_id: int | None = Field(default=None, description="合同 ID")
    file_id: int | None = Field(default=None, description="附件 ID")
    review_task_id: int | None = Field(default=None, description="审查任务 ID")
    sha256: str | None = Field(default=None, description="文件内容 SHA-256")
    file_type: str | None = Field(default=None, description="Backend 识别出的文件类型")

    # ---- 两层幂等的信号（由 Backend 的接入结果透传）----
    reused: bool | None = Field(default=None, description="文件层：是否复用了已有 ContractFile")
    task_reused: bool | None = Field(default=None, description="任务层：是否复用了已有 ReviewTask")

    # ---- Agent 自己的判断 ----
    validation_errors: list[str] = Field(default_factory=list, description="Workflow Gate 给出的不通过原因")
    parse_result: ParseResult | None = Field(
        default=None, description="解析阶段产出。P5-4 仍是 StubParser，text 恒为空"
    )

    # ---- 失败信息 ----
    error_code: str | None = Field(default=None, description="取值见 core.errors.AgentErrorCode")
    error_message: str | None = Field(default=None, description="人话原因")


__all__ = ["ReviewRunResponse"]
