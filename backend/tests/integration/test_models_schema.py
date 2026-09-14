"""P3 模型 ↔ 真实 MySQL 结构一致性测试（**需要真实数据库，且已执行过迁移**）。

与 ``tests/unit/test_models_metadata.py`` 的分工：
* 单元测试断言"模型的定义是对的"；
* 本文件断言"数据库里真实建出来的结构，与模型**逐列一致**"。

MySQL 不可用时整个模块自动跳过。
"""

from __future__ import annotations

import pymysql
import pytest
from sqlalchemy import text
from sqlalchemy.dialects import mysql as mysql_dialect

from app.core.config import get_settings
from app.db import models  # noqa: F401  触发模型注册
from app.db.base import Base
from app.db.session import dispose_engine, get_engine

#: Alembic 自己维护的表，不属于业务模型
ALEMBIC_TABLE = "alembic_version"

#: 类型字符串归一化：SQLAlchemy 编译结果 → information_schema.COLUMN_TYPE 的实际写法。
#: 二者是同一种类型的两种拼写，不是结构差异。
_TYPE_ALIASES = {
    "bool": "tinyint(1)",  # SQLAlchemy Boolean 在 MySQL 里落成 tinyint(1)
    "boolean": "tinyint(1)",
    "integer": "int",  # SQLAlchemy 拼 INTEGER，information_schema 拼 int
}


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
    yield
    await dispose_engine()


async def _fetch_all(sql: str, params: dict | None = None) -> list[tuple]:
    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return list(result.fetchall())


def _expected_mysql_type(col) -> str:
    """把模型的类型编译成 MySQL 的实际列类型写法。

    做两级归一化，覆盖"同类型不同拼写"的差异（**不是**结构差异）：
    ``NUMERIC`` → ``DECIMAL``、``INTEGER`` → ``INT``、``BOOLEAN`` → ``TINYINT(1)``、
    以及去掉参数逗号后的空格（``numeric(18, 2)`` → ``decimal(18,2)``）。
    """
    compiled = col.type.compile(dialect=mysql_dialect.dialect()).lower().replace(", ", ",")
    compiled = compiled.replace("numeric(", "decimal(")
    return _TYPE_ALIASES.get(compiled, compiled)


# --------------------------------------------------------------------------- #
# 表集合
# --------------------------------------------------------------------------- #
async def test_all_model_tables_exist_in_database() -> None:
    rows = await _fetch_all(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :db",
        {"db": get_settings().mysql_db},
    )
    actual = {r[0] for r in rows} - {ALEMBIC_TABLE}
    assert actual == set(Base.metadata.tables), (
        f"数据库缺少: {set(Base.metadata.tables) - actual} / 多出: {actual - set(Base.metadata.tables)}"
    )


async def test_no_extra_business_tables() -> None:
    """除 15 张已批准的表与 alembic_version 外，不允许存在其它表。"""
    rows = await _fetch_all(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :db",
        {"db": get_settings().mysql_db},
    )
    actual = {r[0] for r in rows} - {ALEMBIC_TABLE}
    assert len(actual) == 15


# --------------------------------------------------------------------------- #
# 列类型 / nullable —— 逐列比对
# --------------------------------------------------------------------------- #
async def test_every_column_matches_model_type_and_nullability() -> None:
    db = get_settings().mysql_db
    rows = await _fetch_all(
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :db",
        {"db": db},
    )
    actual = {(r[0], r[1]): (r[2].lower(), r[3]) for r in rows}

    mismatches: list[str] = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            key = (table.name, col.name)
            if key not in actual:
                mismatches.append(f"{key} 在数据库中不存在")
                continue

            db_type, db_nullable = actual[key]
            expected_type = _expected_mysql_type(col)
            if db_type != expected_type:
                mismatches.append(f"{key} 类型: 模型={expected_type} 数据库={db_type}")

            expected_nullable = "YES" if col.nullable else "NO"
            if db_nullable != expected_nullable:
                mismatches.append(f"{key} nullable: 模型={expected_nullable} 数据库={db_nullable}")

    assert not mismatches, "模型与数据库结构不一致：\n  " + "\n  ".join(mismatches)


# --------------------------------------------------------------------------- #
# 唯一约束 / 索引 / 外键
# --------------------------------------------------------------------------- #
async def test_unique_constraints_match_models() -> None:
    db = get_settings().mysql_db
    rows = await _fetch_all(
        "SELECT TABLE_NAME, INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = :db AND NON_UNIQUE = 0 AND INDEX_NAME <> 'PRIMARY' "
        "GROUP BY TABLE_NAME, INDEX_NAME",
        {"db": db},
    )
    actual = {(r[0], tuple(sorted(r[2].split(",")))) for r in rows}
    assert actual == _expected_unique_column_sets(), (
        f"缺少: {_expected_unique_column_sets() - actual} / 多出: {actual - _expected_unique_column_sets()}"
    )


def _expected_unique_column_sets() -> set[tuple[str, tuple[str, ...]]]:
    """从模型推导出应存在的唯一列集合（包含列级 unique 与复合唯一）。"""
    expected: set[tuple[str, tuple[str, ...]]] = set()
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if col.unique:
                expected.add((table.name, (col.name,)))
        for constraint in table.constraints:
            if type(constraint).__name__ == "UniqueConstraint":
                expected.add((table.name, tuple(sorted(c.name for c in constraint.columns))))
    return expected


async def test_documented_indexes_exist() -> None:
    """§7.2 明确点名的两个索引必须存在，且列顺序一致。"""
    db = get_settings().mysql_db
    rows = await _fetch_all(
        "SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'review_task' "
        "GROUP BY INDEX_NAME",
        {"db": db},
    )
    mapping = {r[0]: r[1] for r in rows}
    assert mapping.get("idx_claim") == "status,next_retry_at,priority,id"
    assert mapping.get("idx_heartbeat") == "status,heartbeat_at"


async def test_foreign_keys_match_models_and_reference_correct_tables() -> None:
    db = get_settings().mysql_db
    rows = await _fetch_all(
        "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
        "FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = :db AND REFERENCED_TABLE_NAME IS NOT NULL",
        {"db": db},
    )
    actual = {(r[0], r[1], r[2], r[3]) for r in rows}

    expected = {
        (table.name, fk.parent.name, fk.column.table.name, fk.column.name)
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
    }
    assert actual == expected, f"缺少: {expected - actual} / 多出: {actual - expected}"


async def test_no_foreign_key_cascades_in_database() -> None:
    """决策 O6：数据库中不得存在 CASCADE / SET NULL 等删除行为。"""
    db = get_settings().mysql_db
    rows = await _fetch_all(
        "SELECT CONSTRAINT_NAME, DELETE_RULE, UPDATE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS "
        "WHERE CONSTRAINT_SCHEMA = :db",
        {"db": db},
    )
    offenders = [
        (r[0], r[1], r[2])
        for r in rows
        if r[1] not in ("NO ACTION", "RESTRICT") or r[2] not in ("NO ACTION", "RESTRICT")
    ]
    assert not offenders, f"存在级联删除/更新行为：{offenders}"


# --------------------------------------------------------------------------- #
# 字符集与存储约定
# --------------------------------------------------------------------------- #
async def test_all_tables_use_utf8mb4() -> None:
    db = get_settings().mysql_db
    rows = await _fetch_all(
        "SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA = :db",
        {"db": db},
    )
    bad = [(r[0], r[1]) for r in rows if not str(r[1]).startswith("utf8mb4")]
    assert not bad, f"非 utf8mb4 的表：{bad}"


async def test_specific_column_types_match_architecture_doc() -> None:
    """§7.2 逐字点名的类型，做一次显式断言（不依赖类型编译推导）。"""
    db = get_settings().mysql_db
    expectations = {
        ("contract", "amount"): "decimal(18,2)",
        ("approval_instance", "amount"): "decimal(18,2)",
        ("contract", "currency"): "char(3)",
        ("contract_file", "sha256"): "char(64)",
        ("review_task", "progress"): "tinyint",
        ("ai_call_log", "raw_response"): "mediumtext",
        ("document_block", "bbox_json"): "json",
        ("review_rule", "expression"): "json",
        ("contract", "sign_date"): "date",
        ("contract", "created_at"): "datetime(3)",
        ("risk_item", "updated_at"): "datetime(3)",
    }
    for (table, column), expected_type in expectations.items():
        rows = await _fetch_all(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t AND COLUMN_NAME = :c",
            {"db": db, "t": table, "c": column},
        )
        assert rows, f"{table}.{column} 不存在"
        assert rows[0][0].lower() == expected_type, (
            f"{table}.{column} 期望 {expected_type}，实际 {rows[0][0]}"
        )
