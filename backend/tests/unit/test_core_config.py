"""core/config.py 单元测试。

覆盖：.env 加载、密码隔离（不得出现在 repr / model_dump / safe_summary）、
DSN 拼装与 URL 编码、以及架构文档 §1.3 规定的参数约束。
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import DEFAULT_ENV_FILE, Settings, get_settings

FAKE_PASSWORD = "FakePassw0rdXYZ"


def test_env_file_is_resolved_from_source_location() -> None:
    """`.env` 位置由 __file__ 反推，不依赖进程工作目录。"""
    assert DEFAULT_ENV_FILE.exists(), f".env not found at {DEFAULT_ENV_FILE}"
    assert DEFAULT_ENV_FILE.name == ".env"


def test_reads_values_from_env() -> None:
    s = get_settings()
    assert s.mysql_db == "contract_approval"
    assert s.mysql_port == 3306
    assert s.api_v1_prefix == "/api/v1"
    assert s.log_format == "json"


def test_password_is_secret_str() -> None:
    assert isinstance(get_settings().mysql_password, SecretStr)


def test_password_never_leaks_in_repr_or_dump() -> None:
    s = Settings(mysql_password=FAKE_PASSWORD)
    assert FAKE_PASSWORD not in repr(s)
    assert FAKE_PASSWORD not in str(s)
    assert FAKE_PASSWORD not in json.dumps(s.model_dump(), default=str)


def test_safe_summary_contains_no_secret() -> None:
    s = Settings(mysql_password=FAKE_PASSWORD)
    dumped = json.dumps(s.safe_summary(), default=str)
    assert FAKE_PASSWORD not in dumped
    assert "******" in s.database_url_safe


def test_dsn_url_encodes_special_characters() -> None:
    """密码含 @ : / 时必须 URL 编码，否则 DSN 解析会错位。"""
    s = Settings(mysql_password="p@ss:word/1")
    assert s.database_url.startswith("mysql+aiomysql://root:p%40ss%3Aword%2F1@")
    assert s.database_url.endswith("?charset=utf8mb4")


def test_sync_dsn_uses_pymysql() -> None:
    assert Settings().database_url_sync.startswith("mysql+pymysql://")


def test_safe_dsn_masks_password_without_url_encoding_it() -> None:
    """回归：掩码 `******` 不能参与 URL 编码，否则会变成 %2A%2A 而不可读。"""
    safe = Settings(mysql_password=FAKE_PASSWORD).database_url_safe
    assert FAKE_PASSWORD not in safe
    assert "******" in safe
    assert "%2A" not in safe


def test_empty_password_produces_no_colon() -> None:
    url = Settings(mysql_password="").database_url
    assert "root@" in url and "root:@" not in url


@pytest.mark.parametrize("bad", [0, 3, 5, 8])
def test_ocr_max_workers_rejects_values_outside_1_2_4(bad: int) -> None:
    """架构文档 §1.3：只允许 1 / 2 / 4，避免 Worker × 进程池 造成进程爆炸。"""
    with pytest.raises(ValidationError):
        Settings(ocr_max_workers=bad)


@pytest.mark.parametrize("ok", [1, 2, 4])
def test_ocr_max_workers_accepts_allowed_values(ok: int) -> None:
    assert Settings(ocr_max_workers=ok).ocr_max_workers == ok


def test_log_level_is_validated_and_normalized() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="LOUD")
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_pool_size_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        Settings(db_pool_size=-1)


def test_app_env_is_constrained() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="staging")
    assert Settings(app_env="prod").is_prod is True
