"""业务异常体系。

架构文档：§2.1 core/errors.py（业务异常体系 ErrorCode）、§13.1 错误分类、§8 错误响应约定。

设计要点
--------
1. **错误码枚举化**：§8 要求「所有错误响应带业务 ``code``，前端据此映射提示，
   不靠 message 字符串匹配」。因此每个错误都有一个稳定的 ``ErrorCode``，
   文案可以改，code 不能改。
2. **错误分类内建**：每个 code 都绑定一个 ``ErrorCategory``（§13.1），
   P6 的 ErrorClassifier 直接据此决定「阻塞 / 退避重试 / 降级 / 告警」，
   避免在业务代码里到处写 ``if "超时" in str(e)``。
3. **本模块只做「基座」**：定义错误码、异常类与序列化。
   **FastAPI 全局异常处理器与统一响应体的注册属于 P4**，此处不涉及。

分类与处理策略（架构文档 §13.1）
--------------------------------
==============  ==========  ==============================================
分类             可重试      处理
==============  ==========  ==============================================
USER_ERROR      ✗           直接 blocked，等人工修正（如文件加密）
DATA_ERROR      ✗           降级处理 + 记 warning，**不阻塞任务**
SYSTEM_ERROR    ✓           指数退避自动重试，耗尽后 blocked
FATAL           ✗           blocked + 告警（运维问题，非合同问题）
==============  ==========  ==============================================
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.logging import get_context

# =========================================================================== #
# 错误分类
# =========================================================================== #


class ErrorCategory(StrEnum):
    """错误分类（架构文档 §13.1），决定重试与阻塞策略。"""

    USER_ERROR = "USER_ERROR"  # 用户/数据输入问题，重试无意义，直接阻塞等人工
    DATA_ERROR = "DATA_ERROR"  # 数据质量问题，可降级处理，不阻塞任务
    SYSTEM_ERROR = "SYSTEM_ERROR"  # 系统/网络/外部服务瞬时故障，可重试
    FATAL = "FATAL"  # 配置错误、依赖缺失等，需人工介入

    @property
    def is_retryable(self) -> bool:
        """是否应当自动重试（§13.1：只有 SYSTEM_ERROR 可重试）。"""
        return self is ErrorCategory.SYSTEM_ERROR

    @property
    def should_block(self) -> bool:
        """是否应让任务进入 blocked 状态等待人工（§13.1）。"""
        return self in (ErrorCategory.USER_ERROR, ErrorCategory.FATAL)


# =========================================================================== #
# 错误码
# =========================================================================== #


class ErrorCode(StrEnum):
    """全量业务错误码。

    命名约定：``<领域>_<问题>``。新增时只允许追加，不得修改既有字面值
    （前端映射表与已落库的日志依赖它们）。
    """

    # ---------------------------- 通用 ---------------------------- #
    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"

    # -------------------------- 基础设施 -------------------------- #
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"

    # ------------------------ 合同与附件 ------------------------ #
    CONTRACT_NOT_FOUND = "CONTRACT_NOT_FOUND"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    FILE_CORRUPTED = "FILE_CORRUPTED"
    FILE_ENCRYPTED = "FILE_ENCRYPTED"
    EMPTY_TEXT = "EMPTY_TEXT"
    DUPLICATE_FILE = "DUPLICATE_FILE"  # sha256 命中已有附件（§17.1 最小核心幂等 ①）

    # ---------------------------- 任务 ---------------------------- #
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_BLOCKED = "TASK_BLOCKED"  # §8 明确点名的错误码
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    TASK_TIMEOUT = "TASK_TIMEOUT"
    TASK_RETRY_EXHAUSTED = "TASK_RETRY_EXHAUSTED"
    TASK_ALREADY_COMPLETED = "TASK_ALREADY_COMPLETED"  # completed 为终态，不可回退（§6.1）
    #: 该任务的审查风险已经写入过，拒绝重复写入（P9-10 幂等）。
    #: ⚠️ 不覆盖、不追加 —— 已落库的风险可能已经带有人工复核结果
    TASK_ALREADY_PERSISTED = "TASK_ALREADY_PERSISTED"

    # ------------------------ 文档层持久化（P10-1） ------------------------ #
    #: 该任务的文档层结果（block/clause/metadata）已经写入过，拒绝重复写入
    DOCUMENT_ALREADY_PERSISTED = "DOCUMENT_ALREADY_PERSISTED"
    #: 附件复用块时，本次解析出的块结构与库里已有的对不上 ——
    #: 按位置硬套会把条款挂到错误的块上，因此整批拒绝
    DOCUMENT_BLOCKS_CONFLICT = "DOCUMENT_BLOCKS_CONFLICT"
    #: 附件的解析状态已是终态（PARSED / FAILED），拒绝改写成另一个终态。
    #: ⚠️ 同值重复写入是允许的（同一文件服务多个任务时必然发生）
    PARSE_STATUS_ALREADY_FINAL = "PARSE_STATUS_ALREADY_FINAL"
    #: 风险写入的前置缺失：任务还没走过文档层（``current_stage`` 未到 ``CLAUSED``）
    DOCUMENT_NOT_PERSISTED = "DOCUMENT_NOT_PERSISTED"

    # ------------------------ 人工复核（P13） ------------------------ #
    #: 目标风险不存在，**或它不属于路径里那个任务**。
    #: ⚠️ 两种情形**共用同一个码**：分开表达等于告诉调用方"这个 id 在别的任务里存在"
    RISK_NOT_FOUND = "RISK_NOT_FOUND"
    #: 人工复核的前置缺失：任务还没审完（``current_stage`` 未到 ``REVIEWED``）。
    #: 与 ``REPORT_NOT_READY`` 是同一条件的两个消费场景，但语义各说各的，
    #: 因此不共用一个码（报告读的是分数，复核写的是结论）
    RISK_REVIEW_NOT_READY = "RISK_REVIEW_NOT_READY"

    # ------------------------ 解析 / OCR ------------------------ #
    OCR_FAILED = "OCR_FAILED"
    OCR_LOW_QUALITY = "OCR_LOW_QUALITY"
    CLAUSE_SEGMENT_FAILED = "CLAUSE_SEGMENT_FAILED"
    ANCHOR_NOT_FOUND = "ANCHOR_NOT_FOUND"  # §10.5：quote 定位失败，降级为条款级

    # ---------------------------- LLM ---------------------------- #
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_SCHEMA_INVALID = "LLM_SCHEMA_INVALID"
    LLM_QUOTE_NOT_FOUND = "LLM_QUOTE_NOT_FOUND"

    # ---------------------------- 规则 ---------------------------- #
    RULE_NOT_FOUND = "RULE_NOT_FOUND"
    RULE_EXPRESSION_INVALID = "RULE_EXPRESSION_INVALID"

    # ------------------------ 回写与审批 ------------------------ #
    WRITEBACK_FAILED = "WRITEBACK_FAILED"  # §8 明确点名的错误码
    WRITEBACK_NOT_READY = "WRITEBACK_NOT_READY"
    WRITEBACK_ALREADY_SUCCESS = "WRITEBACK_ALREADY_SUCCESS"  # 幂等键已成功，拒绝重复写
    APPROVAL_SYSTEM_UNAVAILABLE = "APPROVAL_SYSTEM_UNAVAILABLE"

    # ---------------------------- 报告 ---------------------------- #
    REPORT_NOT_READY = "REPORT_NOT_READY"
    REPORT_GENERATION_FAILED = "REPORT_GENERATION_FAILED"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """错误码的元信息：HTTP 状态码 + 分类 + 默认文案。"""

    http_status: int
    category: ErrorCategory
    message: str


#: 错误码 → 元信息。**新增 ErrorCode 必须同步在此登记**，
#: 未登记的 code 会回退为 INTERNAL_ERROR（500 / FATAL），并在测试中暴露。
ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    # ---------------------------- 通用 ---------------------------- #
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, ErrorCategory.FATAL, "服务内部错误"),
    ErrorCode.VALIDATION_ERROR: ErrorSpec(422, ErrorCategory.USER_ERROR, "请求参数校验失败"),
    ErrorCode.UNAUTHORIZED: ErrorSpec(401, ErrorCategory.USER_ERROR, "未登录或登录已过期"),
    ErrorCode.FORBIDDEN: ErrorSpec(403, ErrorCategory.USER_ERROR, "没有操作权限"),
    ErrorCode.NOT_FOUND: ErrorSpec(404, ErrorCategory.USER_ERROR, "资源不存在"),
    ErrorCode.CONFLICT: ErrorSpec(409, ErrorCategory.USER_ERROR, "资源状态冲突"),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, ErrorCategory.SYSTEM_ERROR, "请求过于频繁，请稍后重试"),
    # -------------------------- 基础设施 -------------------------- #
    # SYSTEM_ERROR ⇒ 可重试：数据库瞬时不可用属于基础设施故障，退避重试有意义
    ErrorCode.DATABASE_UNAVAILABLE: ErrorSpec(503, ErrorCategory.SYSTEM_ERROR, "数据库暂时不可用"),
    # 文件存储（磁盘满 / 权限不足 / 目录不可写）同属基础设施故障，可重试
    ErrorCode.STORAGE_UNAVAILABLE: ErrorSpec(503, ErrorCategory.SYSTEM_ERROR, "文件存储暂时不可用"),
    # ------------------------ 合同与附件 ------------------------ #
    ErrorCode.CONTRACT_NOT_FOUND: ErrorSpec(404, ErrorCategory.USER_ERROR, "合同不存在"),
    ErrorCode.FILE_NOT_FOUND: ErrorSpec(404, ErrorCategory.USER_ERROR, "附件不存在"),
    ErrorCode.FILE_TOO_LARGE: ErrorSpec(413, ErrorCategory.USER_ERROR, "文件超过大小上限"),
    ErrorCode.UNSUPPORTED_FORMAT: ErrorSpec(415, ErrorCategory.USER_ERROR, "不支持的文件格式"),
    ErrorCode.FILE_CORRUPTED: ErrorSpec(422, ErrorCategory.USER_ERROR, "文件已损坏，无法解析"),
    ErrorCode.FILE_ENCRYPTED: ErrorSpec(422, ErrorCategory.USER_ERROR, "文件已加密，无法解析"),
    ErrorCode.EMPTY_TEXT: ErrorSpec(422, ErrorCategory.USER_ERROR, "未能从文档中提取到正文"),
    ErrorCode.DUPLICATE_FILE: ErrorSpec(409, ErrorCategory.USER_ERROR, "该文件已存在，已复用历史解析结果"),
    # ---------------------------- 任务 ---------------------------- #
    ErrorCode.TASK_NOT_FOUND: ErrorSpec(404, ErrorCategory.USER_ERROR, "审查任务不存在"),
    ErrorCode.TASK_BLOCKED: ErrorSpec(409, ErrorCategory.USER_ERROR, "任务已阻塞，需人工处理后重试"),
    ErrorCode.INVALID_STATE_TRANSITION: ErrorSpec(409, ErrorCategory.USER_ERROR, "非法的任务状态流转"),
    ErrorCode.TASK_TIMEOUT: ErrorSpec(504, ErrorCategory.SYSTEM_ERROR, "任务执行超时"),
    ErrorCode.TASK_RETRY_EXHAUSTED: ErrorSpec(409, ErrorCategory.SYSTEM_ERROR, "重试次数已耗尽"),
    ErrorCode.TASK_ALREADY_COMPLETED: ErrorSpec(
        409, ErrorCategory.USER_ERROR, "任务已完成，如需重新审查请新建任务"
    ),
    ErrorCode.TASK_ALREADY_PERSISTED: ErrorSpec(
        409, ErrorCategory.USER_ERROR, "该任务的审查风险已写入，不允许重复写入或覆盖"
    ),
    # ------------------------ 文档层持久化（P10-1） ------------------------ #
    ErrorCode.DOCUMENT_ALREADY_PERSISTED: ErrorSpec(
        409, ErrorCategory.USER_ERROR, "该任务的文档结果已写入，不允许重复写入或覆盖"
    ),
    ErrorCode.DOCUMENT_BLOCKS_CONFLICT: ErrorSpec(
        409, ErrorCategory.USER_ERROR, "该附件已有的文档块结构与本次解析结果不一致"
    ),
    ErrorCode.PARSE_STATUS_ALREADY_FINAL: ErrorSpec(
        409, ErrorCategory.USER_ERROR, "附件的解析状态已是终态，不允许改写"
    ),
    #: 风险写入的前置缺失：任务还没走过文档层（current_stage 未到 CLAUSED）。
    #: 此时写入会产出 clause_id 全为 NULL 的风险，并让文档层永久无法补写
    ErrorCode.DOCUMENT_NOT_PERSISTED: ErrorSpec(
        409, ErrorCategory.USER_ERROR, "该任务尚未持久化文档层结果，无法写入风险"
    ),
    # ------------------------ 人工复核（P13） ------------------------ #
    #: 措辞刻意**不区分**"不存在"与"属于别的任务"
    ErrorCode.RISK_NOT_FOUND: ErrorSpec(404, ErrorCategory.USER_ERROR, "风险项不存在"),
    ErrorCode.RISK_REVIEW_NOT_READY: ErrorSpec(
        409, ErrorCategory.USER_ERROR, "该任务尚未完成风险审查，无法人工复核"
    ),
    # ------------------------ 解析 / OCR ------------------------ #
    ErrorCode.OCR_FAILED: ErrorSpec(422, ErrorCategory.SYSTEM_ERROR, "OCR 识别失败"),
    ErrorCode.OCR_LOW_QUALITY: ErrorSpec(422, ErrorCategory.DATA_ERROR, "扫描件清晰度过低，识别结果不可靠"),
    ErrorCode.CLAUSE_SEGMENT_FAILED: ErrorSpec(
        422, ErrorCategory.DATA_ERROR, "条款切分失败，已降级为逐段切分"
    ),
    ErrorCode.ANCHOR_NOT_FOUND: ErrorSpec(422, ErrorCategory.DATA_ERROR, "原文定位失败，已降级为条款级定位"),
    # ---------------------------- LLM ---------------------------- #
    ErrorCode.LLM_UNAVAILABLE: ErrorSpec(503, ErrorCategory.SYSTEM_ERROR, "模型服务暂时不可用"),
    ErrorCode.LLM_TIMEOUT: ErrorSpec(504, ErrorCategory.SYSTEM_ERROR, "模型调用超时"),
    ErrorCode.LLM_SCHEMA_INVALID: ErrorSpec(502, ErrorCategory.SYSTEM_ERROR, "模型输出未通过结构校验"),
    ErrorCode.LLM_QUOTE_NOT_FOUND: ErrorSpec(
        422, ErrorCategory.DATA_ERROR, "模型引用的原文片段在正文中不存在"
    ),
    # ---------------------------- 规则 ---------------------------- #
    ErrorCode.RULE_NOT_FOUND: ErrorSpec(404, ErrorCategory.USER_ERROR, "审查规则不存在"),
    ErrorCode.RULE_EXPRESSION_INVALID: ErrorSpec(422, ErrorCategory.USER_ERROR, "规则表达式非法"),
    # ------------------------ 回写与审批 ------------------------ #
    ErrorCode.WRITEBACK_FAILED: ErrorSpec(502, ErrorCategory.SYSTEM_ERROR, "审批意见回写失败"),
    ErrorCode.WRITEBACK_NOT_READY: ErrorSpec(409, ErrorCategory.USER_ERROR, "任务尚未审查完成，无法回写"),
    ErrorCode.WRITEBACK_ALREADY_SUCCESS: ErrorSpec(
        409, ErrorCategory.USER_ERROR, "该意见已成功回写，无需重复提交"
    ),
    ErrorCode.APPROVAL_SYSTEM_UNAVAILABLE: ErrorSpec(503, ErrorCategory.SYSTEM_ERROR, "审批系统暂时不可用"),
    # ---------------------------- 报告 ---------------------------- #
    ErrorCode.REPORT_NOT_READY: ErrorSpec(409, ErrorCategory.USER_ERROR, "报告尚未生成"),
    ErrorCode.REPORT_GENERATION_FAILED: ErrorSpec(500, ErrorCategory.SYSTEM_ERROR, "报告生成失败"),
}


# =========================================================================== #
# 异常类
# =========================================================================== #


class AppError(Exception):
    """所有业务异常的基类。

    用法::

        raise NotFoundError("合同 42 不存在", details={"contract_id": 42})

        raise AppError(code=ErrorCode.WRITEBACK_FAILED,
                       message="审批系统返回 502",
                       details={"attempt": 3})
    """

    #: 子类的默认错误码；未登记在 ERROR_SPECS 时回退为 INTERNAL_ERROR
    default_code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code: ErrorCode = code or self.default_code
        spec = ERROR_SPECS.get(self.code)
        if spec is None:
            # 防御：新增了 ErrorCode 却忘了登记 ERROR_SPECS
            spec = ERROR_SPECS[ErrorCode.INTERNAL_ERROR]
        self.spec: ErrorSpec = spec
        self.message: str = message or spec.message
        self.details: dict[str, Any] = details or {}
        super().__init__(self.message)

    # ---------------------------- 便捷属性 ---------------------------- #
    @property
    def http_status(self) -> int:
        return self.spec.http_status

    @property
    def category(self) -> ErrorCategory:
        return self.spec.category

    @property
    def is_retryable(self) -> bool:
        """是否应自动重试（§13.1）。"""
        return self.category.is_retryable

    @property
    def should_block(self) -> bool:
        """是否应使任务进入 blocked（§13.1）。"""
        return self.category.should_block

    # ---------------------------- 序列化 ---------------------------- #
    def to_dict(self) -> dict[str, Any]:
        """统一错误响应体（§8：``{code, message, data, request_id}``）。

        ``request_id`` 取自日志上下文（若入口已 ``bind_context``），便于日志与响应关联排查。
        """
        return {
            "code": str(self.code),
            "message": self.message,
            "details": self.details,
            "request_id": get_context().get("request_id"),
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# ------------------------------- 常用子类 ------------------------------- #


class ValidationError(AppError):
    default_code = ErrorCode.VALIDATION_ERROR


class UnauthorizedError(AppError):
    default_code = ErrorCode.UNAUTHORIZED


class ForbiddenError(AppError):
    default_code = ErrorCode.FORBIDDEN


class NotFoundError(AppError):
    default_code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    default_code = ErrorCode.CONFLICT


class ServiceUnavailableError(AppError):
    """外部依赖（LLM / 审批系统）不可用。"""

    default_code = ErrorCode.LLM_UNAVAILABLE


class TaskStateError(AppError):
    """非法的任务状态流转（§6.1 迁移矩阵校验失败）。"""

    default_code = ErrorCode.INVALID_STATE_TRANSITION


class TaskBlockedError(AppError):
    """任务进入 blocked，需人工处理（§6.1）。"""

    default_code = ErrorCode.TASK_BLOCKED


class FileParseError(AppError):
    """文档解析类错误，具体原因由 ``code`` 区分（加密 / 损坏 / 空正文 / OCR）。"""

    default_code = ErrorCode.FILE_CORRUPTED
