"""core/logging.py 单元测试。

覆盖：JSON 结构化输出、contextvars 全链路上下文、extra 字段合并、
上下文隔离（不得共享可变默认值）、以及 setup_logging 的幂等性。
"""

from __future__ import annotations

import json
import logging as std_logging

import pytest

from app.core import logging as applog


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """每个用例前后都清空日志上下文，避免用例间相互污染。"""
    applog.clear_context()
    yield
    applog.clear_context()


def _json_lines(out: str) -> list[dict]:
    return [json.loads(ln) for ln in out.splitlines() if ln.strip().startswith("{")]


def test_json_log_line_carries_context_and_extra(capsys: pytest.CaptureFixture) -> None:
    applog.setup_logging(level="DEBUG", fmt="json")
    applog.bind_context(request_id="req-123", task_id=42)
    applog.get_logger("app.demo").info("hello", extra={"contract_id": 7})

    records = _json_lines(capsys.readouterr().out)
    assert records, "expected at least one JSON log line"
    rec = records[-1]
    assert rec["level"] == "INFO"
    assert rec["message"] == "hello"
    assert rec["logger"] == "app.demo"
    assert rec["request_id"] == "req-123"
    assert rec["task_id"] == 42
    assert rec["contract_id"] == 7, "extra= fields must be merged"


def test_timestamp_is_utc_iso8601(capsys: pytest.CaptureFixture) -> None:
    applog.setup_logging(level="INFO", fmt="json")
    applog.get_logger("app.demo").info("ts-check")
    rec = _json_lines(capsys.readouterr().out)[-1]
    assert rec["ts"].endswith("Z") or "+00:00" in rec["ts"], rec["ts"]


def test_clear_context_resets_everything() -> None:
    applog.bind_context(request_id="x", task_id=1)
    assert applog.get_context() == {"request_id": "x", "task_id": 1}
    applog.clear_context()
    assert applog.get_context() == {}


def test_bind_context_ignores_none_values() -> None:
    """便于写 bind_context(task_id=maybe_none) 而不污染上下文。"""
    applog.bind_context(request_id="r", task_id=None)
    assert applog.get_context() == {"request_id": "r"}


def test_bind_context_rejects_unknown_fields() -> None:
    """字段白名单：防止日志结构随时间和模块漂移。"""
    with pytest.raises(ValueError, match="未知的日志上下文字段"):
        applog.bind_context(nonsense=1)


def test_context_default_is_not_shared_mutable_state() -> None:
    """回归（ruff B039）：未绑定时拿到的字典不能被原地修改后影响后续读取。"""
    leaked = applog.get_context()
    leaked["injected"] = "bad"
    assert applog.get_context() == {}, "context default leaked across calls"


def test_console_format_is_human_readable(capsys: pytest.CaptureFixture) -> None:
    applog.setup_logging(level="INFO", fmt="console")
    applog.bind_context(stage="PARSE")
    applog.get_logger("app.demo").warning("warn-msg")

    out = capsys.readouterr().out
    assert "warn-msg" in out
    assert "stage=PARSE" in out
    assert not out.strip().startswith("{"), "console format must not be JSON"


def test_exception_traceback_is_captured(capsys: pytest.CaptureFixture) -> None:
    applog.setup_logging(level="INFO", fmt="json")
    try:
        raise ValueError("boom")
    except ValueError:
        applog.get_logger("app.demo").exception("failed")

    rec = _json_lines(capsys.readouterr().out)[-1]
    assert "exception" in rec
    assert "ValueError: boom" in rec["exception"]


def test_setup_logging_is_idempotent(capsys: pytest.CaptureFixture) -> None:
    """uvicorn --reload 会重复导入模块，不能因此叠加 handler 造成日志重复。"""
    applog.setup_logging(level="INFO", fmt="json")
    applog.setup_logging(level="INFO", fmt="json")
    applog.setup_logging(level="INFO", fmt="json")
    assert len(std_logging.getLogger().handlers) == 1


def test_uvicorn_loggers_are_hijacked() -> None:
    applog.setup_logging(level="INFO", fmt="json")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = std_logging.getLogger(name)
        assert lg.handlers == [], f"{name} should propagate to root handler"
        assert lg.propagate is True


def test_non_serializable_extra_does_not_break_logging(capsys: pytest.CaptureFixture) -> None:
    """default=str 兜底：即使传入 Decimal/Enum 也不能丢日志。"""
    applog.setup_logging(level="INFO", fmt="json")
    applog.get_logger("app.demo").info("odd", extra={"amount": object()})
    rec = _json_lines(capsys.readouterr().out)[-1]
    assert "amount" in rec
