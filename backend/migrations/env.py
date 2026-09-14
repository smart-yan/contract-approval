"""Alembic 运行环境（异步）。

架构文档：§2.1 migrations/env.py、§17 的 P2 交付项「Alembic 初始化与异步迁移配置」。

设计要点
--------
1. **DSN 只有一个来源**：``app.core.config.Settings``（即 .env）。
   alembic.ini 里不写 ``sqlalchemy.url``，避免密码出现第二份副本、也避免两处配置不一致
   导致迁移跑到错误的库上。

2. **迁移引擎与业务引擎相互独立**：
   这里**不复用** ``app.db.session`` 的引擎。迁移是一次性、单连接的过程，
   复用业务引擎的连接池只会让连接长期驻留。因此：
   * 在线模式：新建引擎，``poolclass=NullPool``（用完即断，不留常驻连接）；
   * 离线模式：用同步 DSN 生成 SQL，根本不需要驱动连接。

3. **target_metadata 指向 Base.metadata**（``app.db.base``），
   因此 P3 起新增的每个模型只要继承 ``Base`` 就会被 autogenerate 感知。

4. **compare_type / compare_server_default 打开**：
   让 autogenerate 能发现「列类型变化」「默认值变化」，
   否则改字段类型时 Alembic 会静默生成空迁移。

5. **在线迁移的会话时区同样固定为 UTC**（``build_connect_args``）。
   离线模式只渲染 SQL 文本、不存在数据库会话，因此**不对它做任何时区包装** ——
   伪造一个时区只会让生成的 SQL 与实际执行环境不一致。
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

# 保证 `alembic` 从仓库根目录或 backend/ 目录执行时都能 import 到 app 包
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings
from app.db.base import Base, build_connect_args

# Alembic 的 Config 对象（alembic.ini 的内容）
config = context.config

# 应用 alembic.ini 中的日志配置（若存在）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: autogenerate 的对比目标。P3 起新增模型会自动被纳入。
target_metadata = Base.metadata

settings = get_settings()


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """过滤 autogenerate 的对比对象。

    当前唯一规则：忽略 Alembic 自己的版本表（它由 Alembic 管理，不该出现在迁移里）。
    """
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库（``alembic upgrade head --sql``）。

    使用**同步 DSN**：这个模式不需要异步驱动，只要能确定方言即可。
    """
    context.configure(
        url=settings.database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """在给定连接上执行迁移（由 ``run_sync`` 从异步连接桥接过来）。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：用异步引擎执行迁移。

    ``NullPool``：迁移是一次性操作，用完立即断开，不在连接池里留下常驻连接
    （与业务引擎的池化策略刻意相反）。

    ``connect_args`` 与运行期引擎**共用** ``build_connect_args()``：
    迁移期间执行的 DDL/DML 也必须落在 UTC 会话时区下，
    否则诸如 ``server_default=func.now()`` 之类由数据库求值的表达式会写入本地时间，
    与项目 naive UTC 约定不一致。
    """
    connectable = create_async_engine(
        settings.database_url,
        poolclass=pool.NullPool,
        connect_args=build_connect_args(),
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
