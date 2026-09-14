"""Agent 侧的错误码词汇表。

与 Backend 的关系
----------------
Backend 的 ``app/core/errors.py`` 是一整套异常体系（错误码 + 分类 + 重试策略 +
FastAPI 异常处理器）。Agent **不复制那一套** —— P5 阶段 Agent 对错误只有一个诉求：
让 Graph 的 Conditional Edge 有一个**稳定、可断言**的判断依据。

因此这里只有一张最小的字符串枚举，没有异常类、没有 HTTP 状态码、没有错误分类。

为什么写进 State 而不是抛异常
---------------------------
"上传失败"（格式不支持、超过大小上限）是**可预期的业务结果**。
把它抛成异常会让整个 Graph 中断，Conditional Edge 也就失去了存在意义。
节点把失败写进 State，由 Conditional Edge 决定走向 —— 这正是引入它的原因。

P6 引入 ErrorClassifier 后，"Backend 暂时不可达"这类瞬时故障会改为退避重试，
而不是直接判定为 invalid。P5-3 刻意不做这件事。
"""

from __future__ import annotations

from enum import StrEnum


class AgentErrorCode(StrEnum):
    """Agent 侧错误码。取值会被写入 ``ContractReviewState.error_code``。"""

    #: 调用方没有给全 Workflow 必需的输入（用错了 Graph，不必发请求）
    AGENT_INPUT_INVALID = "AGENT_INPUT_INVALID"

    #: Backend 不可达 / 超时（SYSTEM_ERROR，P6 后应改为退避重试）
    BACKEND_UNREACHABLE = "BACKEND_UNREACHABLE"

    #: Backend 返回非 2xx，且响应体不是可解析的统一错误结构
    BACKEND_REJECTED = "BACKEND_REJECTED"

    #: Backend 返回 2xx，但响应体缺少继续 Workflow 所必需的字段
    BACKEND_CONTRACT_INCOMPLETE = "BACKEND_CONTRACT_INCOMPLETE"


__all__ = ["AgentErrorCode"]
