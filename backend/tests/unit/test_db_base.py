"""db/base.py 单元测试。

覆盖：约束命名规范、公共 Mixin 的字段定义、DATETIME(3) 精度、
以及两条建模纪律 —— **不建业务表**（P2-c 范围守卫）、**不使用 MySQL 原生 ENUM**。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, MetaData, inspect
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase

from app.db.base import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    NAMING_CONVENTION,
    UTC_SESSION_INIT_COMMAND,
    Base,
    BaseMixin,
    build_connect_args,
)
from app.utils.datetime_utils import utcnow


class _ProbeBase(DeclarativeBase):
    """独立 metadata 的探针基类。

    刻意**不复用** ``Base``：否则探针表会被注册进 ``Base.metadata``，
    污染"当前还没有业务表"的守卫用例。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Probe(_ProbeBase, BaseMixin):
    __tablename__ = "p2c_probe"


def test_naming_convention_covers_all_constraint_kinds() -> None:
    """命名规范必须覆盖 ix/uq/ck/fk/pk，否则 Alembic 会生成不可预测的约束名。"""
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}


def test_pk_name_follows_naming_convention() -> None:
    assert _Probe.__table__.primary_key.name == "pk_p2c_probe"


def test_mixin_provides_id_created_at_updated_at() -> None:
    cols = _Probe.__table__.c
    assert set(cols.keys()) == {"id", "created_at", "updated_at"}


def test_id_is_bigint_autoincrement_primary_key() -> None:
    col = _Probe.__table__.c.id
    assert isinstance(col.type, BigInteger)
    assert col.primary_key is True
    assert col.autoincrement is True


def test_timestamps_use_datetime_with_millisecond_precision() -> None:
    """架构文档 §7.2：DATETIME(3)，UTC 存储。"""
    for name in ("created_at", "updated_at"):
        col = _Probe.__table__.c[name]
        assert isinstance(col.type, DATETIME), f"{name} should be MySQL DATETIME"
        assert col.type.fsp == 3, f"{name} should have fsp=3"
        assert col.nullable is False


def test_updated_at_has_onupdate_hook() -> None:
    assert _Probe.__table__.c.updated_at.onupdate is not None
    assert _Probe.__table__.c.created_at.onupdate is None


def test_python_side_defaults_return_naive_utc() -> None:
    """列默认值必须是 naive UTC 的 datetime。

    注意：SQLAlchemy 会把零参可调用对象包装成 ``(ctx) -> value``，
    所以这里用 ``arg(None)`` 触发它。
    """
    for name in ("created_at", "updated_at"):
        default = _Probe.__table__.c[name].default
        assert default is not None and callable(default.arg)
        value = default.arg(None)
        assert isinstance(value, datetime)
        assert value.tzinfo is None, "写库必须是 naive UTC，aware datetime 会被驱动错误序列化"


def test_no_mysql_native_enum_is_used() -> None:
    """建模纪律：枚举字段一律用 VARCHAR 存字面值，不用 MySQL 原生 ENUM。"""
    for col in _Probe.__table__.c:
        assert not isinstance(col.type, __import__("sqlalchemy").Enum), f"{col.name} uses Enum type"


def test_probe_table_is_not_registered_in_shared_metadata() -> None:
    """探针表不进入 Base.metadata，保证守卫用例的语义不被破坏。"""
    assert "p2c_probe" not in Base.metadata.tables


def test_business_tables_are_registered_by_p3_models() -> None:
    """P3 起：``Base.metadata`` 由 ``app.db.models`` 注册的模型填充。

    【本用例的前身是 P2-c 的 ``test_no_business_tables_exist_yet``】
    那个守卫断言"P3 之前 Base.metadata 必须为空"，并**在 docstring 里预先声明**
    它会在 P3 添加第一个模型时失败、届时应改写为对具体表结构的断言而非删除。
    现在正是那一刻，故改写为下面的形式：

    * ``app.db.models`` 是模型注册的唯一入口，必须能填充 metadata；
    * 表清单的完整断言在 ``tests/unit/test_models_metadata.py``，
      此处只守住"注册链路本身是通的"这一条，避免两个文件重复维护同一份表清单。
    """
    from app.db import models  # noqa: F401  确保注册入口被导入

    assert Base.metadata.tables, "P3 之后 Base.metadata 不应为空 —— 模型注册链路断了"
    assert "contract" in Base.metadata.tables
    assert "review_task" in Base.metadata.tables


def test_metadata_carries_naming_convention() -> None:
    assert Base.metadata.naming_convention == NAMING_CONVENTION


def test_utcnow_is_naive_and_tracks_utc() -> None:
    """utcnow 必须是"不带 tzinfo 的 UTC 时间"，且落在两个 UTC 采样点之间。

    刻意与 ``datetime.now(UTC)`` 对比而不是与本地时间对比 ——
    否则在 UTC+8 机器上会得到 8 小时偏移却依然通过。
    """
    before = datetime.now(UTC).replace(tzinfo=None)
    value = utcnow()
    after = datetime.now(UTC).replace(tzinfo=None)

    assert value.tzinfo is None
    assert before <= value <= after, f"utcnow() 不在 UTC 采样区间内：{before} <= {value} <= {after}"


def test_inspect_sees_expected_table_shape() -> None:
    """用 SQLAlchemy 的 inspect 再确认一次表结构（不依赖方言反射）。"""
    mapper = inspect(_Probe)
    assert mapper.local_table.name == "p2c_probe"
    assert {c.key for c in mapper.columns} == {"id", "created_at", "updated_at"}


# --------------------------------------------------------------------------- #
# 约定 4：数据库会话时区固定为 UTC
# --------------------------------------------------------------------------- #
def test_utc_session_init_command_targets_utc() -> None:
    assert UTC_SESSION_INIT_COMMAND.startswith("SET time_zone")
    assert "+00:00" in UTC_SESSION_INIT_COMMAND


def test_build_connect_args_pins_utc_session_time_zone() -> None:
    """所有 MySQL 连接都必须经由此函数构造，否则会绕过 UTC 会话时区约定。"""
    args = build_connect_args()
    assert args["init_command"] == UTC_SESSION_INIT_COMMAND
    assert args["connect_timeout"] == DEFAULT_CONNECT_TIMEOUT_SECONDS


def test_build_connect_args_accepts_custom_timeout() -> None:
    assert build_connect_args(connect_timeout=3)["connect_timeout"] == 3
    # 时区设置不因超时参数而丢失
    assert build_connect_args(connect_timeout=3)["init_command"] == UTC_SESSION_INIT_COMMAND
