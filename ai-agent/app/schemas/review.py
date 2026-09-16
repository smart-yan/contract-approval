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
        description="本工作流是否**成功走完且没有失败标记**。"
        "completed = 上传通过 + 解析拿到了可用文档（PARSED 或 EMPTY）+ ``error_code`` 为空；"
        "rejected = 其余情况：被 validate 门禁拦下、解析失败、解析根本没跑过，"
        "或**后续节点判定失败**（如规则审查缺少规则快照这种输入缺失）。"
        "具体原因看 error_code —— status 只回答'成没成功'，不回答'为什么没成功'"
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


class ReviewAcceptedResponse(BaseModel):
    """`POST /api/agent/review` 的 **202 响应**（P14-4）。

    为什么只有一个字段
    ----------------
    因为在这个时刻，**别的都还不知道**：图还没开始跑，没有 ``workflow_status``、
    没有 ``parse_result``、没有风险。任何"预计耗时""队列位置""进度"都只能是编的。

    调用方拿 ``task_id`` 去 Backend 查真实状态 —— 那是任务的**事实来源**
    （``GET /api/v1/review-tasks/{id}/workbench``）。

    ⚠️ 刻意**不含** ``status`` / ``progress`` / ``polling_url`` / ``estimated_time``：
    这些要么与 Backend 的字段重复（两处口径迟早漂移），要么是猜的。
    它们属于 P14-5 之后再看的事。

    ⚠️ ``task_id`` 是**预上传**时由 Backend 创建的、**真实的** ReviewTask id，
    不是 Agent 自己编的关联号 —— 后台图跑起来后 ``upload_file`` 会因
    ``sha256`` 幂等命中同一个任务，不会另建一个（见 P14-4 的幂等验证）。
    """

    task_id: int = Field(description="Backend 的 ReviewTask ID —— 用它去查进度与结果")


__all__ = ["ReviewAcceptedResponse", "ReviewRunResponse"]
