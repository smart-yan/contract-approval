"""审批回写的**服务层契约**（P15-2）。

本阶段的边界
-----------
P15-2 只做「**持久化语义**」：门禁 → 生成正文 → 落 ``writeback_record`` → 推进
``WritebackStatus``。**不调用审批系统**（真实或 Mock 都没有），因此这里没有
"调用结果"这类字段 —— 那些属于 P15-3 的 ``ApprovalClient``。

为什么分成"发起"与"结束"两次调用
------------------------------
§6.2 的状态机是 ``not_written → writing → success / failed``，而"writing"这段
**包含一次网络调用**（往审批系统发评论）。项目有一条硬规则：**事务里不许夹长耗时
网络调用**（见 ``db/session.py`` 对 ``session_scope`` 的说明）。因此：

::

    start_writeback()   →  落库：writing，attempt+1，started_at        （短事务，结束）
        ↓                  （P15-3 在这里调 ApprovalClient）
    finish_writeback()  →  落库：success / failed，finished_at        （短事务，结束）

这样"审批系统超时"不会把一个数据库事务挂住，「写成功了但响应丢了」也仍然由
``idempotency_key`` 兜住（正文与键在**发起**时就定好了，重试拿到的是同一个键）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class WritebackStartResponse(BaseModel):
    """一次回写尝试**发起**后的状态，以及给它准备好的正文。

    ``content_md`` 由调用方（P15-3 的 ``ApprovalClient``）拿去发评论 —— 它就是
    这条 ``writeback_record`` 的快照内容，两者**逐字节相同**（同一次渲染的结果）。
    """

    record_id: int = Field(description="writeback_record 主键")
    task_id: int = Field(description="所属审查任务")
    contract_id: int = Field(description="所属合同")
    approval_instance_id: int = Field(description="目标审批单")
    status: str = Field(description="回写状态，取值见 constants.WritebackStatus")

    attempt: int = Field(description="含本次在内的已尝试次数（首次为 1）")
    started: bool = Field(
        description="本次调用是否**新发起**了一次尝试。"
        "``False`` 表示复用了已存在且处于 ``writing`` 的记录（例如重复点击、"
        "上一次还没回来）—— 此时**不要**再往审批系统发一次评论"
    )

    content_md: str = Field(description="回写正文（Markdown 快照）")
    content_hash: str = Field(description="content_md 的 SHA-256")
    idempotency_key: str = Field(description="sha256(task_id + content_hash)，UNIQUE")


class WritebackFinishResponse(BaseModel):
    """一次回写尝试**结束**后的状态。"""

    record_id: int = Field(description="writeback_record 主键")
    task_id: int = Field(description="所属审查任务")
    status: str = Field(description="``success`` 或 ``failed``")
    attempt: int = Field(description="本次尝试的序号")
    external_comment_id: str | None = Field(default=None, description="审批系统返回的评论 ID（成功时才有）")
    error_msg: str | None = Field(default=None, description="失败原因（失败时才有）")
    finished_at: datetime | None = Field(default=None, description="结束时刻（naive UTC）")


class WritebackRunResponse(BaseModel):
    """**一次完整的回写调用**（含外部审批系统交互）的结果（P15-3b）。

    与 :class:`WritebackFinishResponse` 的区别：那个是"本地这件事结束了"，
    这个是"**连同外部系统在内**，这次调用做完了什么"。差别就在 ``posted``。
    """

    record_id: int = Field(description="writeback_record 主键")
    task_id: int = Field(description="所属审查任务")
    approval_instance_id: int = Field(
        description="目标审批单。⚠️ 由后端从 ``contract.approval_instance_id`` 解析，"
        "**不接受调用方传入**（P15-3b）"
    )
    status: str = Field(description="本地回写状态，取值见 constants.WritebackStatus")
    attempt: int = Field(description="含本次在内的已尝试次数")
    external_comment_id: str | None = Field(default=None, description="审批系统里那条评论的 ID（成功时才有）")
    error_msg: str | None = Field(default=None, description="失败原因（失败时才有）")
    finished_at: datetime | None = Field(default=None, description="结束时刻（naive UTC）")
    posted: bool = Field(
        description="本次调用是否**真的向审批系统发出了写请求**。"
        "``False`` 且 ``status=success`` 表示：外部早已有这条评论（上一次写成功但响应丢了），"
        "本次只是把它认了回来 —— **没有产生第二条评论**"
    )


__all__ = [
    "WritebackFinishResponse",
    "WritebackRunResponse",
    "WritebackStartResponse",
]
