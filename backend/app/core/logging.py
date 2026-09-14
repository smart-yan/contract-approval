"""结构化日志基座。

架构文档：§2.1 core/logging.py（结构化 JSON 日志）、§13.6 日志。

设计要点
--------
1. **JSON 输出**：每条日志一行 JSON，可直接被 ELK / Loki / CloudWatch 采集，
   不需要正则解析文本。``ensure_ascii=False`` 保证中文可读。
2. **全链路上下文**：``request_id`` / ``task_id`` / ``contract_id`` / ``stage``
   通过 ``contextvars`` 传递（§13.6 要求这些字段贯穿全链路），
   调用方只需在入口处 ``bind_context(...)`` 一次，后续所有日志自动携带。
   使用 ``contextvars`` 而非全局字典，是为了在 asyncio 并发任务之间天然隔离。
3. **本地可读**：``LOG_FORMAT=console`` 时切换为单行文本格式，便于本地开发。
4. **接管 uvicorn 日志**：让 uvicorn / uvicorn.access 也走同一套 formatter，
   避免同一份日志里出现两种格式。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# --------------------------------------------------------------------------- #
# 全链路日志上下文
# --------------------------------------------------------------------------- #
# default 用 None 而非 {}：可变对象作为 ContextVar 默认值会被所有未绑定的上下文共享，
# 一旦有人原地修改就会污染全局默认值（ruff B039）。本模块一律「取副本、整体 set」。
_log_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)

#: 上下文中允许出现的标准字段（架构文档 §13.6）。用集合约束，避免随手塞入无关字段。
CONTEXT_FIELDS: frozenset[str] = frozenset(
    {"request_id", "task_id", "contract_id", "file_id", "stage", "worker_id", "operator"}
)


def bind_context(**kwargs: Any) -> None:
    """绑定日志上下文。入口处调用一次，后续所有日志自动携带。

    例：``bind_context(request_id="req-1", task_id=42)``

    值为 ``None`` 的键会被忽略（便于 ``bind_context(task_id=maybe_none)`` 这种写法）。
    """
    unknown = set(kwargs) - CONTEXT_FIELDS
    if unknown:
        raise ValueError(f"未知的日志上下文字段：{sorted(unknown)}；允许：{sorted(CONTEXT_FIELDS)}")

    ctx = dict(_log_context.get() or {})
    ctx.update({k: v for k, v in kwargs.items() if v is not None})
    _log_context.set(ctx)


def clear_context() -> None:
    """清空日志上下文。

    **必须在请求/任务结束时调用**（放在 ``finally`` 里），否则在复用协程或线程池的
    场景下，上一个请求的 ``request_id`` 会串到下一个请求的日志里。
    """
    _log_context.set({})


def get_context() -> dict[str, Any]:
    """返回当前上下文的副本（只读用途）。"""
    return dict(_log_context.get() or {})


# --------------------------------------------------------------------------- #
# Formatter
# --------------------------------------------------------------------------- #

#: LogRecord 的内置属性，序列化时需要排除，只保留调用方通过 extra= 传入的自定义字段
_RESERVED_ATTRS: frozenset[str] = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


def _iter_extra(record: logging.LogRecord) -> dict[str, Any]:
    """取出 ``logger.info("msg", extra={"task_id": 1})`` 中传入的自定义字段。"""
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED_ATTRS and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # 统一以 UTC 落库（ISO8601），避免跨机器/跨时区日志无法对齐
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 上下文先写入，extra 后写入 —— 允许单条日志临时覆盖上下文值
        payload.update(_log_context.get() or {})
        payload.update(_iter_extra(record))

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # default=str 兜底：即使传入了 datetime / Enum / Decimal 也不会因序列化失败而丢日志
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """本地开发用的单行可读格式：时间 | 级别 | logger | 消息 | k=v 上下文。"""

    def format(self, record: logging.LogRecord) -> str:
        # 显式 UTC → 本地时区转换：本地阅读用，与 JSON 格式的 UTC 时间戳语义一致
        ts = datetime.fromtimestamp(record.created, tz=UTC).astimezone().strftime("%H:%M:%S")
        base = f"{ts} | {record.levelname:<8} | {record.name:<28} | {record.getMessage()}"

        fields = {**(_log_context.get() or {}), **_iter_extra(record)}
        if fields:
            base += " | " + " ".join(f"{k}={v}" for k, v in fields.items())

        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #

#: 需要接管的第三方 logger（让其日志也走同一套 formatter）
_HIJACKED_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access")


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    """初始化根 logger。**应在应用启动时调用一次**（P2-d 的 main.py 中调用）。

    参数缺省时从 ``Settings`` 读取（``LOG_LEVEL`` / ``LOG_FORMAT``）。
    延迟导入 config 是为了避免 core 内部模块循环导入。
    """
    from app.core.config import get_settings

    settings = get_settings()
    log_level = (level or settings.log_level).upper()
    log_format = fmt or settings.log_format

    formatter: logging.Formatter = JsonFormatter() if log_format == "json" else ConsoleFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # 幂等：重复调用时不叠加 handler（uvicorn --reload 会重复导入模块）
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(log_level)

    # 接管 uvicorn 自己的 handler，统一格式并避免重复输出
    for name in _HIJACKED_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers = []
        third_party.propagate = True

    logging.getLogger("app").debug(
        "logging 初始化完成", extra={"log_level": log_level, "log_format": log_format}
    )


def get_logger(name: str) -> logging.Logger:
    """获取 logger。约定：业务模块统一用 ``get_logger(__name__)``。"""
    return logging.getLogger(name)
