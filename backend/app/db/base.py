"""SQLAlchemy 声明式基类与公共 Mixin。

架构文档：§2.1 db/base.py（DeclarativeBase + 公共 Mixin(id/created_at/updated_at)）、
§7.2 表设计约定。

本模块**只提供基座，不定义任何业务表**（业务 Model 属于 P3）。

四条贯穿全项目的建模约定
------------------------
1. **约束命名规范（naming_convention）**
   MySQL 会自动给未命名的约束起名（如 ``contract_ibfk_1``），这类名字**不可预测**，
   会导致 Alembic 自动生成的迁移在某些机器上删不掉约束。这里统一约定命名，
   让约束名由表名/列名确定性地推导出来。

   ⚠️ 必须在**生成第一个迁移之前**设定。等表建好再改，就得手写迁移重命名所有约束。

2. **不使用 MySQL 原生 ENUM 类型**
   架构文档 §7.2 明确各枚举字段为 ``VARCHAR``。Python 侧用 ``StrEnum``（见
   ``app.core.constants``）保证类型安全，DB 侧存字符串字面值。
   理由：MySQL 原生 ENUM 增删枚举值必须 ``ALTER TABLE``（大表上是锁表操作），
   且值集合被固化在表结构里，跨环境迁移容易不一致。

3. **时间列统一 DATETIME(3) 存 UTC**（见 ``app.utils.datetime_utils``）。

4. **数据库会话时区固定为 UTC**（见下方 ``build_connect_args``）。

   MySQL 的 ``NOW()`` / ``CURRENT_TIMESTAMP`` 返回的是**会话时区**的当前时间。
   若会话时区跟随服务器 SYSTEM（中文环境通常是 UTC+8），而我们写入的是 naive UTC，
   两者会相差 8 小时，且**故障是静默的**：

   * §1.4 抢任务 SQL ``next_retry_at <= NOW(3)``：退避时间会提前 8 小时满足 → 任务被过早重试；
   * §13.5 心跳自愈 ``heartbeat_at < NOW() - 5min``：所有在跑任务都"看起来"过期 8 小时
     → 自愈器把全部任务打回 pending，造成重复执行甚至活锁。

   因此所有 MySQL 连接（运行期与在线迁移）都通过 ``build_connect_args()`` 建立，
   在连接建立时执行 ``SET time_zone = '+00:00'``，让数据库侧与 Python 侧共用同一个时间基准。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, MetaData
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.datetime_utils import utcnow

# --------------------------------------------------------------------------- #
# 连接参数：会话时区固定为 UTC
# --------------------------------------------------------------------------- #
#: 在每条连接建立时执行，把会话时区钉死为 UTC。
#: 用 ``init_command`` 而不是连接后再执行一条 ``SET``，是为了保证**连接池里每一条连接**
#: （包括后续新建补充的连接）都自带该设置，不会出现"漏设"的连接。
UTC_SESSION_INIT_COMMAND = "SET time_zone = '+00:00'"

#: 建立 TCP 连接的超时（秒）。数据库不可达时快速失败，不让请求长时间挂起。
DEFAULT_CONNECT_TIMEOUT_SECONDS: int = 10


def build_connect_args(*, connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """返回统一的 MySQL 连接参数（运行期引擎与 Alembic 在线迁移引擎共用）。

    **所有**创建 MySQL 连接的地方都必须经过这里，否则就会绕开 UTC 会话时区约定。
    """
    return {
        "connect_timeout": connect_timeout,
        "init_command": UTC_SESSION_INIT_COMMAND,
    }


# --------------------------------------------------------------------------- #
# 约束命名规范
# --------------------------------------------------------------------------- #
#: 让索引/唯一约束/检查约束/外键/主键的名字都可确定性推导，便于 Alembic 生成与回滚。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全项目唯一的声明式基类。

    所有业务模型都必须继承它（P3 起）：

        class Contract(Base, BaseMixin):
            __tablename__ = "contract"
            ...

    Alembic 的 ``target_metadata`` 直接指向 ``Base.metadata``（见 ``migrations/env.py``）。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class BaseMixin:
    """公共字段 Mixin：``id`` / ``created_at`` / ``updated_at``（架构文档 §7.2）。

    「所有表含 id BIGINT PK AUTO_INCREMENT、created_at、updated_at（DATETIME(3)，UTC 存储）」。

    作为普通 Python 类使用（不继承 ``Base``），业务模型这样组合::

        class Contract(Base, BaseMixin):
            __tablename__ = "contract"
    """

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="主键",
    )

    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3),
        default=utcnow,
        nullable=False,
        comment="创建时间（UTC）",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="更新时间（UTC）",
    )
