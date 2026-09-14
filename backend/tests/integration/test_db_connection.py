"""数据库连接与 Session 生命周期的集成测试（**需要真实 MySQL**）。

MySQL 不可用（服务未启动 / 未配置密码 / 库不存在）时，整个模块**自动跳过**，
不会让单元测试套件变红。跳过原因会打印出来，可以从 pytest 输出里直接看到。

覆盖：
* 连通性与服务端信息（版本 / 字符集 / 当前库）
* ``session_scope()`` 正常提交后连接归还连接池
* ``session_scope()`` 异常时**数据层面真的回滚**（用一次性探针表实证，用完即删）
* 连接池用完后 ``checkedout`` 归零（没有连接泄漏）
"""

from __future__ import annotations

import pymysql
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import build_connect_args
from app.db.session import check_connection, dispose_engine, get_engine, session_scope
from app.utils.datetime_utils import utcnow

logger = get_logger(__name__)

#: 一次性探针表名。加前缀避免与业务表冲突，测试结束必定 DROP。
PROBE_TABLE = "_p2c_rollback_probe"


def _db_available() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.mysql_password.get_secret_value():
        return False, ".env 中未配置 MYSQL_PASSWORD"
    try:
        conn = pymysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            database=settings.mysql_db,
            charset="utf8mb4",
            connect_timeout=3,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}（MySQL 服务未启动或库不存在？）"
    else:
        conn.close()
        return True, ""


_AVAILABLE, _REASON = _db_available()

pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=f"MySQL 不可用：{_REASON}")


@pytest.fixture(autouse=True)
async def _cleanup_engine():
    """集成测试结束后释放引擎与探针表，避免影响其它测试。"""
    yield
    try:
        async with session_scope() as session:
            await session.execute(text(f"DROP TABLE IF EXISTS {PROBE_TABLE}"))
    except Exception as exc:  # noqa: BLE001 - 清理失败不应掩盖真正的断言失败
        logger.warning("清理探针表失败", extra={"error_type": type(exc).__name__})
    await dispose_engine()


async def test_check_connection_reports_server_info() -> None:
    info = await check_connection()
    settings = get_settings()

    assert info["ok"] is True
    assert info["server_version"].startswith("8."), f"期望 MySQL 8.x，实际 {info['server_version']}"
    assert info["charset"] == "utf8mb4"
    assert info["database"] == settings.mysql_db
    # 返回的连接串必须是脱敏的
    assert settings.mysql_password.get_secret_value() not in info["url"]
    assert "***" in info["url"]


async def test_session_scope_executes_query() -> None:
    async with session_scope() as session:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_connection_returns_to_pool_after_use() -> None:
    pool = get_engine().pool
    async with session_scope() as session:
        await session.execute(text("SELECT 1"))
    assert pool.checkedout() == 0, "session_scope 退出后连接必须归还连接池"


async def test_session_scope_rolls_back_on_exception() -> None:
    """数据层面实证回滚：异常路径写入的行不能留在表里。

    用一次性探针表而不是真实业务表 —— P2-c 还没有任何业务表。
    """
    async with session_scope() as session:
        await session.execute(text(f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE} (id INT PRIMARY KEY)"))
        await session.execute(text(f"DELETE FROM {PROBE_TABLE}"))

    with pytest.raises(RuntimeError, match="boom"):
        async with session_scope() as session:
            await session.execute(text(f"INSERT INTO {PROBE_TABLE} (id) VALUES (1)"))
            raise RuntimeError("boom")

    async with session_scope() as session:
        remaining = (await session.execute(text(f"SELECT COUNT(*) FROM {PROBE_TABLE}"))).scalar_one()
    assert remaining == 0, "异常必须回滚，探针表里不应留下数据"


async def test_session_scope_commits_on_success() -> None:
    async with session_scope() as session:
        await session.execute(text(f"CREATE TABLE IF NOT EXISTS {PROBE_TABLE} (id INT PRIMARY KEY)"))
        await session.execute(text(f"DELETE FROM {PROBE_TABLE}"))

    async with session_scope() as session:
        await session.execute(text(f"INSERT INTO {PROBE_TABLE} (id) VALUES (2)"))

    async with session_scope() as session:
        rows = (await session.execute(text(f"SELECT COUNT(*) FROM {PROBE_TABLE}"))).scalar_one()
    assert rows == 1, "正常退出应当提交"


async def test_distinct_sessions_are_independent() -> None:
    """两次 session_scope 拿到的是不同 Session，且都能正常工作。"""
    async with session_scope() as first, session_scope() as second:
        assert first is not second
        assert (await first.execute(text("SELECT 1"))).scalar_one() == 1
        assert (await second.execute(text("SELECT 2"))).scalar_one() == 2


# --------------------------------------------------------------------------- #
# 会话时区：必须与项目的 naive UTC + DATETIME(3) 约定一致
# --------------------------------------------------------------------------- #
async def test_session_time_zone_is_utc() -> None:
    """MySQL NOW() 返回会话时区的时间；会话时区必须是 +00:00。"""
    async with session_scope() as session:
        tz = (await session.execute(text("SELECT @@session.time_zone"))).scalar_one()
    assert tz == "+00:00", f"期望会话时区 +00:00，实际 {tz}"


async def test_now_matches_utc_timestamp() -> None:
    """NOW(3) 与 UTC_TIMESTAMP(3) 必须基本一致（差额远小于 1 秒）。"""
    async with session_scope() as session:
        now3 = (await session.execute(text("SELECT NOW(3)"))).scalar_one()
        utc3 = (await session.execute(text("SELECT UTC_TIMESTAMP(3)"))).scalar_one()
    assert abs((now3 - utc3).total_seconds()) < 1, f"NOW(3)={now3} 与 UTC_TIMESTAMP(3)={utc3} 不一致"


async def test_utcnow_matches_database_utc_clock() -> None:
    """Python 侧 utcnow() 与数据库侧 UTC 时钟语义一致（允许数秒网络/执行偏差）。"""
    async with session_scope() as session:
        db_utc = (await session.execute(text("SELECT UTC_TIMESTAMP(3)"))).scalar_one()
    py_utc = utcnow()
    assert abs((db_utc - py_utc).total_seconds()) < 5, f"DB={db_utc} 与 utcnow()={py_utc} 偏差过大"


async def test_time_zone_survives_pool_checkout_and_reuse() -> None:
    """连接池复用/新建连接后，会话时区仍然正确（init_command 是逐连接生效的）。"""
    engine = get_engine()
    # 取用次数刻意超过池大小，迫使连接池既复用旧连接、也新建补充连接
    rounds = engine.pool.size() + get_settings().db_max_overflow + 2
    seen: set[str] = set()
    for _ in range(rounds):
        async with engine.connect() as conn:
            seen.add((await conn.execute(text("SELECT @@session.time_zone"))).scalar_one())
    assert seen == {"+00:00"}, f"池内出现未设置 UTC 的连接：{seen}"


async def test_migration_engine_also_uses_utc_session_time_zone() -> None:
    """Alembic 在线迁移引擎与运行期引擎共用 build_connect_args()，会话时区同样为 UTC。"""
    engine = create_async_engine(
        get_settings().database_url,
        poolclass=NullPool,
        connect_args=build_connect_args(),
    )
    try:
        async with engine.connect() as conn:
            tz = (await conn.execute(text("SELECT @@session.time_zone"))).scalar_one()
            now3 = (await conn.execute(text("SELECT NOW(3)"))).scalar_one()
            utc3 = (await conn.execute(text("SELECT UTC_TIMESTAMP(3)"))).scalar_one()
    finally:
        await engine.dispose()

    assert tz == "+00:00"
    assert abs((now3 - utc3).total_seconds()) < 1
