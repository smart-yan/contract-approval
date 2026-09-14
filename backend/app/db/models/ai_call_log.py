"""LLM 调用日志模型：``ai_call_log``（架构文档 §7.2、§13.6）。

**这是面试重点**：每一次模型调用都全量留痕 —— provider / model / 场景 /
prompt 版本 / token 用量 / 耗时 / 重试序号 / 原始响应。
有了它才能回答"改了 prompt 之后效果变好还是变差"这类问题。

设计说明
--------
* ``raw_response`` 用 MySQL 的 ``MEDIUMTEXT``（§7.2 明确指定），可容纳 16MB 文本。
* token 用量、HTTP 状态等允许为空：调用失败时没有这些数据。
* LLM Provider 与调用链路属于 **P10**，本模块只建表。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseMixin


class AiCallLog(Base, BaseMixin):
    """模型调用日志（§7.2 ai_call_log）。"""

    __tablename__ = "ai_call_log"

    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_task.id"), nullable=False, comment="所属审查任务"
    )

    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="提供方，如 deepseek / anthropic"
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False, comment="模型标识")
    scene: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="调用场景，取值见 constants.LLMScene（§9.1）"
    )
    prompt_version: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="提示词版本，用于效果回归对比"
    )

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="输入 token 数")
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="输出 token 数")
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="总 token 数")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="耗时（毫秒）")

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="调用结果状态，如 success / failed"
    )
    retry_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="第几次重试（0 表示首次）"
    )

    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="HTTP 状态码")
    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="错误码，取值见 constants/errors.ErrorCode"
    )
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误详情（脱敏）")

    request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="链路追踪 ID，可与应用日志对齐"
    )
    raw_response: Mapped[str | None] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="模型原始响应（含 JSON 围栏），排查与回归用"
    )
