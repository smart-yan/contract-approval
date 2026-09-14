"""core/errors.py 单元测试。

覆盖：错误码登记完整性、HTTP 状态与分类映射、§13.1 的重试/阻塞语义、
统一错误响应体（§8）与 request_id 关联。
"""

from __future__ import annotations

import pytest

from app.core import logging as applog
from app.core.errors import (
    ERROR_SPECS,
    AppError,
    ErrorCategory,
    ErrorCode,
    FileParseError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    TaskBlockedError,
    TaskStateError,
    ValidationError,
)


def test_every_error_code_is_registered() -> None:
    """新增 ErrorCode 却忘记登记 ERROR_SPECS 时，此用例直接失败。"""
    missing = [code for code in ErrorCode if code not in ERROR_SPECS]
    assert not missing, f"codes without ERROR_SPECS: {missing}"


def test_error_specs_are_internally_consistent() -> None:
    for code, spec in ERROR_SPECS.items():
        assert 400 <= spec.http_status <= 599, f"{code}: bad http status {spec.http_status}"
        assert spec.message, f"{code}: empty default message"


def test_not_found_error_maps_to_404_user_error() -> None:
    err = NotFoundError("contract 42 missing", details={"contract_id": 42})
    assert err.code == ErrorCode.NOT_FOUND
    assert err.http_status == 404
    assert err.category == ErrorCategory.USER_ERROR
    assert err.is_retryable is False

    body = err.to_dict()
    assert body["code"] == "NOT_FOUND"
    assert body["message"] == "contract 42 missing"
    assert body["details"] == {"contract_id": 42}


def test_task_blocked_error_is_409_and_not_retryable() -> None:
    err = TaskBlockedError()
    assert err.http_status == 409
    assert err.should_block is True
    assert err.is_retryable is False


def test_system_errors_are_retryable() -> None:
    assert ServiceUnavailableError().is_retryable is True


def test_encrypted_file_blocks_instead_of_retrying() -> None:
    """§13.1：加密属于 USER_ERROR，重试无意义，直接阻塞等人工换文件。"""
    err = FileParseError(code=ErrorCode.FILE_ENCRYPTED)
    assert err.is_retryable is False
    assert err.should_block is True


def test_data_errors_degrade_without_blocking() -> None:
    """§13.1：数据质量问题走降级，绝不能让整个任务卡住。"""
    assert ErrorCategory.DATA_ERROR.is_retryable is False
    assert ErrorCategory.DATA_ERROR.should_block is False


def test_fatal_errors_block_and_are_not_retryable() -> None:
    assert ErrorCategory.FATAL.is_retryable is False
    assert ErrorCategory.FATAL.should_block is True


def test_default_message_comes_from_spec() -> None:
    err = AppError(code=ErrorCode.WRITEBACK_FAILED)
    assert err.message == ERROR_SPECS[ErrorCode.WRITEBACK_FAILED].message
    assert err.http_status == 502
    assert err.category == ErrorCategory.SYSTEM_ERROR


def test_class_level_default_codes() -> None:
    assert ValidationError().http_status == 422
    assert ForbiddenError().http_status == 403
    assert TaskStateError().http_status == 409
    assert TaskStateError().code == ErrorCode.INVALID_STATE_TRANSITION


def test_request_id_flows_from_log_context_into_error_body() -> None:
    """响应体携带 request_id，便于把用户报错与后端日志对上。"""
    applog.bind_context(request_id="req-xyz")
    try:
        assert NotFoundError().to_dict()["request_id"] == "req-xyz"
    finally:
        applog.clear_context()


def test_request_id_is_none_when_no_context_bound() -> None:
    applog.clear_context()
    assert NotFoundError().to_dict()["request_id"] is None


def test_app_error_is_a_real_exception() -> None:
    with pytest.raises(AppError) as exc_info:
        raise NotFoundError("nope")
    assert exc_info.value.code == ErrorCode.NOT_FOUND


def test_repr_exposes_code_but_keeps_details_out() -> None:
    text = repr(NotFoundError("x", details={"secret": "should-not-be-here"}))
    assert "NOT_FOUND" in text
    assert "should-not-be-here" not in text
