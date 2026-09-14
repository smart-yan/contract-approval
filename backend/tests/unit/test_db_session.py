"""db/session.py 单元测试（不需要真实数据库）。

覆盖：引擎懒加载与单例语义、dispose 后的生命周期重置、连接池参数是否正确下发、
密码不泄露（DSN 脱敏 + 异常文本清洗）、以及"Session 不是全局单例"。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

import app.db.session as session_module
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.base import UTC_SESSION_INIT_COMMAND
from app.db.session import (
    check_connection,
    dispose_engine,
    get_engine,
    get_sessionmaker,
    safe_url,
    scrub_password,
)

FAKE_PASSWORD = "TotallyFakePwd9876"


@pytest.fixture(autouse=True)
async def _dispose_after_each_test():
    """每个用例后释放引擎，避免引擎状态在用例之间串味。"""
    yield
    await dispose_engine()


# --------------------------------------------------------------------------- #
# 引擎生命周期
# --------------------------------------------------------------------------- #
async def test_engine_is_a_process_wide_singleton() -> None:
    assert get_engine() is get_engine()


async def test_engine_is_not_created_at_import_time() -> None:
    """导入模块不得建立引擎 —— 否则 CLI / Alembic 只要 import 就会被牵连。"""
    await dispose_engine()
    assert session_module._engine is None


async def test_dispose_resets_engine_so_a_new_one_is_built() -> None:
    first = get_engine()
    await dispose_engine()
    assert session_module._engine is None

    second = get_engine()
    assert second is not first, "dispose 之后必须重建引擎，而不是复用已释放的实例"


async def test_sessionmaker_is_rebuilt_after_dispose() -> None:
    first_factory = get_sessionmaker()
    await dispose_engine()
    second_factory = get_sessionmaker()
    assert second_factory is not first_factory
    assert second_factory.kw["bind"] is get_engine()


# --------------------------------------------------------------------------- #
# 连接池参数
# --------------------------------------------------------------------------- #
async def test_pool_settings_are_forwarded_to_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接池参数必须与 .env 一致（§17.2：DB 连接池配置是 P2 交付项）。"""
    captured: dict[str, Any] = {}
    real_create = session_module.create_async_engine

    def _spy(url: str, **kwargs: Any) -> AsyncEngine:
        captured["url"] = url
        captured.update(kwargs)
        return real_create(url, **kwargs)

    monkeypatch.setattr(session_module, "create_async_engine", _spy)
    await dispose_engine()
    get_engine()

    settings = get_settings()
    assert captured["pool_size"] == settings.db_pool_size
    assert captured["max_overflow"] == settings.db_max_overflow
    assert captured["pool_recycle"] == settings.db_pool_recycle
    assert captured["pool_timeout"] == settings.db_pool_timeout
    assert captured["echo"] == settings.db_echo
    assert captured["pool_pre_ping"] is True
    assert captured["pool_use_lifo"] is True
    assert captured["connect_args"]["connect_timeout"] == session_module.CONNECT_TIMEOUT_SECONDS
    # 会话时区必须由统一的 build_connect_args() 注入，不能有引擎绕过它
    assert captured["connect_args"]["init_command"] == UTC_SESSION_INIT_COMMAND


async def test_pool_recycle_is_shorter_than_mysql_wait_timeout_default() -> None:
    """pool_recycle 必须小于 MySQL wait_timeout(默认 28800s)，否则会拿到已关闭的连接。"""
    assert 0 < get_settings().db_pool_recycle < 28800


async def test_real_engine_pool_reflects_settings() -> None:
    engine = get_engine()
    assert engine.pool.size() == get_settings().db_pool_size


# --------------------------------------------------------------------------- #
# 密码隔离
# --------------------------------------------------------------------------- #
async def test_engine_url_hides_password_in_repr() -> None:
    engine = get_engine()
    rendered = repr(engine.url)
    assert get_settings().mysql_password.get_secret_value() not in rendered
    assert "***" in rendered


async def test_safe_url_is_masked() -> None:
    masked = safe_url(get_engine())
    assert get_settings().mysql_password.get_secret_value() not in masked
    assert "***" in masked


def test_scrub_password_removes_raw_and_url_encoded_forms() -> None:
    from urllib.parse import quote_plus

    password = get_settings().mysql_password.get_secret_value()
    if not password:
        pytest.skip("未配置密码，跳过清洗用例")

    assert password not in scrub_password(f"boom url=mysql://root:{password}@h/db")
    assert quote_plus(password) not in scrub_password(f"boom {quote_plus(password)}")


async def test_connection_failure_does_not_leak_password() -> None:
    """连不上的库必须有脱敏的错误，且异常链里不能带出 DSN。

    使用一个**专门构造的坏引擎**（假密码 + 必定拒绝连接的端口），
    这样既验证了行为，又完全不触碰真实凭据。
    """
    bad_engine = create_async_engine(
        f"mysql+aiomysql://nobody:{FAKE_PASSWORD}@127.0.0.1:1/nodb",
        poolclass=NullPool,
        connect_args={"connect_timeout": 2},
    )
    try:
        with pytest.raises(AppError) as exc_info:
            await check_connection(bad_engine)
    finally:
        await bad_engine.dispose()

    err = exc_info.value
    assert err.code == ErrorCode.DATABASE_UNAVAILABLE
    assert err.http_status == 503
    assert err.category.value == "SYSTEM_ERROR"

    payload = json.dumps(err.to_dict(), default=str)
    assert FAKE_PASSWORD not in payload
    assert FAKE_PASSWORD not in str(err)
    assert FAKE_PASSWORD not in repr(err)
    assert err.details["database"].count("***") >= 1


# --------------------------------------------------------------------------- #
# Session 不是单例
# --------------------------------------------------------------------------- #
async def test_sessionmaker_yields_distinct_sessions() -> None:
    factory = get_sessionmaker()
    first, second = factory(), factory()
    try:
        assert first is not second, "Session 绝不能是全局单例"
        assert first.bind is second.bind
    finally:
        await first.close()
        await second.close()


def test_sessionmaker_is_a_factory_not_a_session() -> None:
    """async_sessionmaker 可以全局复用；它产出的 Session 才是每事务一个。"""
    assert callable(get_sessionmaker())
    assert not hasattr(get_sessionmaker(), "execute")
