"""审查任务的**状态写入口**契约（P14-4）。

为什么单独一个模块
----------------
``review_task`` 的读契约散在 ``contract.py``（列表摘要）与 ``workbench.py``（工作台）里，
都是**投影**。这里是第一个"由调用方推动任务状态"的写契约，语义与那些读模型无关，
因此单独放一处，而不是塞进任何一个读模型旁边。

为什么只有 BLOCKED，没有通用的 ``/status``
----------------------------------------
Agent 在后台执行整张图（P14-4 起），失败时**必须**有个地方如实记下"这次跑挂了"。
它需要的只有这一件事：**把任务标记为阻塞并写清原因**。

一个通用的状态写接口会让调用方能把任务改成任意状态 —— 那是把状态机的权威
从 Backend 挪到调用方。而状态机（``TASK_STATUS_TRANSITIONS``）是 §6.1 声明过的
**唯一权威**，不该开一个能绕过它的后门。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TaskBlockRequest(BaseModel):
    """把任务标记为阻塞的请求体。

    两个字段都**必填**：``block_reason_code`` 是给程序看的（分类、统计、自愈扫描），
    ``block_reason_msg`` 是给人看的（运维、排查）。只给其中之一都会让另一类读者
    拿到一个无法处理的空值 —— 这正是 §6.1 把两者分开存的原因。
    """

    block_reason_code: str = Field(
        min_length=1,
        max_length=32,
        description="阻塞原因枚举，取值见 constants.BlockReasonCode（§6.1）。"
        "⚠️ 列宽 32，与 ``review_task.block_reason_code`` 一致",
    )
    block_reason_msg: str = Field(
        min_length=1,
        max_length=512,
        description="人话原因，前端与运维直接读。⚠️ **不要放 traceback** —— "
        "这一列会经 API 回到调用方与界面；堆栈留在日志里。列宽 512",
    )


class TaskBlockResponse(BaseModel):
    """阻塞写入后该任务的实际状态。"""

    task_id: int = Field(description="审查任务 ID")
    status: str = Field(description="更新后的任务状态，取值见 constants.TaskStatus（应为 blocked）")
    current_stage: str = Field(
        description="阶段标记。⚠️ **本接口刻意不改它** —— 阶段表达的是"
        "「已完成到哪一步」（断点续跑依据），任务失败不代表阶段回退或前进"
    )
    block_reason_code: str = Field(description="已写入的阻塞原因枚举")
    block_reason_msg: str = Field(description="已写入的人话原因")


__all__ = ["TaskBlockRequest", "TaskBlockResponse"]
