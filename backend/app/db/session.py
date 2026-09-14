"""异步数据库引擎与会话生命周期。

架构文档：§1.3 并发模型、§1.4 DB Queue 抢占的事务边界、§13.4 超时、§17.2（DB 连接池配置）。

四条硬性原则
------------
1. **引擎不在 import 期创建**（懒加载）。导入本模块不得建立任何连接，
   否则 CLI 脚本 / Alembic / 单元测试只要 import 就会被牵连。
   进程退出时由应用入口（P2-d 的 lifespan）调用 ``dispose_engine()`` 释放连接池。

2. **Session 绝不是全局单例**。每请求 / 每事务一个 Session：
   * HTTP 请求 → ``get_db_session()``（FastAPI 依赖）
   * 后台任务 / 脚本 → ``session_scope()``
   ``async_sessionmaker`` 只是"会话工厂"，可以全局复用；Session 实例不可以。

3. **长耗时操作期间绝不持有 Session / 事务**（§1.4）。
   OCR、LLM 调用可能耗时数分钟，用 Session 包住它们会长时间占用连接、
   拉长事务、并让连接池迅速耗尽。正确形态是「取数据 → 关会话 → 干重活 → 开新会话写结果」：

   ✅ 正确::

       async with session_scope() as s:          # 短事务 1：读
           clauses = await load_clauses(s, task_id)

       risks = await call_llm(clauses)           # 无 Session，长耗时

       async with session_scope() as s:          # 短事务 2：写
           await save_risks(s, risks)

   ❌ 错误::

       async with session_scope() as s:
           risks = await call_llm(clauses)       # 事务被拖住数分钟

4. **连接失败不得泄露密码**。对外只暴露 ``engine.url.render_as_string(hide_password=True)``，
   异常文本经 ``scrub_password()`` 清洗后才记录或抛出。

连接池参数与并发模型的一致性（§1.3）
------------------------------------
* 部署形态是 **2 个 worker 进程**（``WORKER_CONCURRENCY`` 默认 2）× 每进程一个引擎。
* 默认 ``DB_POOL_SIZE=10`` + ``DB_MAX_OVERFLOW=20`` ⇒ 单进程峰值 30 连接，
  两个 worker 合计 60 —— 需要 MySQL ``max_connections``（默认 151）留有余量。
* ``pool_recycle`` 默认 3600s，必须**小于** MySQL ``wait_timeout``，
  否则会拿到服务端已关闭的连接；``pool_pre_ping`` 作为二次保险。
* ``pool_use_lifo=True``：优先复用最近释放的连接，让空闲连接更快被回收，
  契合"任务突发、平时空闲"的 worker 负载形态。

会话时区（与 naive UTC 约定对齐）
---------------------------------
连接参数统一由 ``app.db.base.build_connect_args()`` 构造，其中包含
``SET time_zone = '+00:00'``。原因：MySQL 的 ``NOW()`` 返回**会话时区**的时间，
若会话时区跟随服务器 SYSTEM（中文环境通常 UTC+8），就会与我们写入的 naive UTC
相差 8 小时，且故障是静默的 —— §1.4 的 ``next_retry_at <= NOW(3)`` 会提前 8 小时命中，
§13.5 的心跳自愈会把所有在跑任务误判为过期。详见 ``app.db.base`` 的约定 4。
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.db.base import DEFAULT_CONNECT_TIMEOUT_SECONDS, build_connect_args

logger = get_logger(__name__)

#: 建立 TCP 连接的超时（秒）。避免数据库不可达时请求长时间挂起。
CONNECT_TIMEOUT_SECONDS: int = DEFAULT_CONNECT_TIMEOUT_SECONDS

# --------------------------------------------------------------------------- #
# 模块级状态：引擎与会话工厂（懒加载，进程内各一份）
# --------------------------------------------------------------------------- #
_engine: AsyncEngine | None = None
_engine_lock = threading.Lock()
_session_factory: async_sessionmaker[AsyncSession] | None = None


def scrub_password(message: str) -> str:
    """从任意文本中抹掉数据库密码（含 URL 编码形态）。

    作为**兜底防线**：即便某条异常信息意外带上了 DSN，也不会把密码写进日志或响应体。
    """
    password = get_settings().mysql_password.get_secret_value()
    if not password:
        return message
    for variant in {password, quote_plus(password)}:
        if variant and variant in message:
            message = message.replace(variant, "******")
    return message


def safe_url(engine: AsyncEngine) -> str:
    """返回脱敏后的 DSN，可安全写日志 / 返回给调用方。"""
    return engine.url.render_as_string(hide_password=True)


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
def get_engine() -> AsyncEngine:
    """返回进程内唯一的异步引擎（首次调用时创建，线程安全）。

    **只创建引擎对象，不建立连接** —— 真正的连接由连接池在第一次执行 SQL 时按需建立。
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                settings = get_settings()
                try:
                    engine = create_async_engine(
                        settings.database_url,
                        echo=settings.db_echo,
                        # ---- 连接池（与 §1.3 并发模型对齐，见模块 docstring）----
                        pool_size=settings.db_pool_size,
                        max_overflow=settings.db_max_overflow,
                        pool_recycle=settings.db_pool_recycle,
                        pool_timeout=settings.db_pool_timeout,
                        pool_pre_ping=True,
                        pool_use_lifo=True,
                        # ---- 连接级参数 ----
                        # 经 build_connect_args() 统一构造：包含把会话时区钉死为 UTC 的
                        # init_command。任何绕过它的引擎都会破坏 §1.4 / §13.5 的时间比较。
                        connect_args=build_connect_args(connect_timeout=CONNECT_TIMEOUT_SECONDS),
                    )
                except Exception as exc:  # noqa: BLE001 - DSN 非法等配置错误
                    # 兜底：解析 DSN 失败时，异常文本可能包含完整 URL
                    raise AppError(
                        code=ErrorCode.DATABASE_UNAVAILABLE,
                        message=f"数据库配置有误：{scrub_password(str(exc))}",
                        details={"database": settings.database_url_safe},
                    ) from None

                _engine = engine
                logger.info(
                    "数据库引擎已创建",
                    extra={
                        "database": safe_url(engine),
                        "pool_size": settings.db_pool_size,
                        "max_overflow": settings.db_max_overflow,
                        "pool_recycle": settings.db_pool_recycle,
                    },
                )
    return _engine


async def dispose_engine() -> None:
    """释放连接池并清空模块状态。

    应用关闭时调用（P2-d 的 lifespan）。调用后再 ``get_engine()`` 会重新创建一个新引擎。
    **测试也依赖这一点**来隔离引擎状态。
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("数据库连接池已释放", extra={"database": safe_url(_engine)})
    _engine = None
    _session_factory = None


# --------------------------------------------------------------------------- #
# 会话工厂与 Session 生命周期
# --------------------------------------------------------------------------- #
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """返回进程内唯一的会话工厂（绑定到当前引擎）。

    ``async_sessionmaker`` 本身无状态、可安全复用；**它产出的 Session 才是每事务一个**。
    """
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            # commit 后不过期对象属性。异步场景下访问过期属性会触发隐式 IO，
            # 抛 MissingGreenlet；关掉它可以避免这类难以定位的错误。
            expire_on_commit=False,
            # 关闭自动 flush：何时把变更推到数据库由业务代码显式决定，避免"莫名报错"。
            autoflush=False,
        )
    return _session_factory


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个 Session。

    * 请求结束（无论成功失败）自动关闭；
    * 抛异常时自动 rollback；
    * **不自动 commit** —— 是否提交由 service 层显式决定。

    用法（P4 起）::

        @router.get("/contracts")
        async def list_contracts(session: AsyncSession = Depends(get_db_session)):
            ...
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """独立短事务：正常退出 commit，异常 rollback，无论如何都关闭。

    供**非 HTTP 场景**使用：worker 阶段执行、CLI 脚本、初始化逻辑。

    ⚠️ **严禁用它包住 OCR / LLM 等长耗时调用**（见模块 docstring 第 3 条）。
    一个 ``async with`` 块 = 一个短事务，块内只做数据库读写。
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# --------------------------------------------------------------------------- #
# 连通性检查
# --------------------------------------------------------------------------- #
async def check_connection(engine: AsyncEngine | None = None) -> dict[str, Any]:
    """执行一次 ``SELECT`` 验证连通性，成功返回安全可打印的信息。

    失败时抛出 ``AppError(DATABASE_UNAVAILABLE)``，**其 message 与 details 均已脱敏**。

    :param engine: 可选，指定引擎（测试用）；默认使用进程内引擎。
    """
    target = engine or get_engine()
    url = safe_url(target)
    try:
        async with target.connect() as conn:
            version = (await conn.execute(text("SELECT VERSION()"))).scalar_one()
            charset = (await conn.execute(text("SELECT @@character_set_connection"))).scalar_one()
            database = (await conn.execute(text("SELECT DATABASE()"))).scalar_one()
        return {
            "ok": True,
            "url": url,
            "server_version": str(version),
            "charset": str(charset),
            "database": str(database),
        }
    except SQLAlchemyError as exc:
        error_type = type(exc).__name__
        logger.warning(
            "数据库连接检查失败",
            extra={"database": url, "error_type": error_type},
        )
        raise AppError(
            code=ErrorCode.DATABASE_UNAVAILABLE,
            message="数据库连接失败，请检查 MySQL 服务是否启动以及 .env 中的连接配置",
            details={"database": url, "error_type": error_type},
        ) from None
