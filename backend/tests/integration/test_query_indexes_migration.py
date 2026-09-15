"""查询索引 migration 的**真实往返验证**（P11-2，需要真实 MySQL）。

为什么必须跑真库
--------------
索引不是 ORM 声明出来的（本步明确禁止改 Model），它**只存在于 migration 里** ——
因此"索引对不对"这件事在代码里根本看不出来，只能真的迁移一次、查
``information_schema`` 才知道。

⚠️ **本模块会真的切换数据库的 migration 版本**（head ⇄ baseline）。
它在每个用例前后都把库**恢复到 head**，并且断言里带 ``finally`` ——
否则一次失败就会把库留在 baseline，让后面所有用例莫名其妙地失败。

⚠️ 与既有测试的关系：``tests/integration/test_models_schema.py`` 只断言
"§7.2 点名的两个索引存在"，**不比较索引集合**，因此本模块新增的 5 个索引
不会让它失败（已确认）。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pymysql
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]

#: 本步新增的 5 个索引：名字 → (表, 期望的列顺序)
EXPECTED_INDEXES: dict[str, tuple[str, str]] = {
    "ix_clause_task_id": ("clause", "task_id"),
    "ix_contract_metadata_task_id": ("contract_metadata", "task_id"),
    "ix_risk_item_task_id": ("risk_item", "task_id"),
    "ix_review_task_contract_id_id": ("review_task", "contract_id,id"),
    "ix_contract_created_at_id": ("contract", "created_at,id"),
}

#: 会被本次迁移**接管**的外键索引（升级后它们应当消失，回滚后应当回来）
FK_INDEXES = {
    "clause": "fk_clause_task_id_review_task",
    "contract_metadata": "fk_contract_metadata_task_id_review_task",
    "risk_item": "fk_risk_item_task_id_review_task",
    "review_task": "fk_review_task_contract_id_contract",
}


def _connect() -> pymysql.connections.Connection:
    from app.core.config import get_settings

    settings = get_settings()
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password.get_secret_value(),
        database=settings.mysql_db,
        charset="utf8mb4",
        connect_timeout=5,
    )


def _db_available() -> tuple[bool, str]:
    settings_password = None
    try:
        from app.core.config import get_settings

        settings_password = get_settings().mysql_password.get_secret_value()
    except Exception as exc:  # noqa: BLE001
        return False, f"读取配置失败：{type(exc).__name__}"
    if not settings_password:
        return False, ".env 中未配置 MYSQL_PASSWORD"
    try:
        _connect().close()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}（MySQL 服务未启动或库不存在？）"
    return True, ""


_AVAILABLE, _REASON = _db_available()
pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=f"MySQL 不可用：{_REASON}")


def _query(sql: str, params: tuple = ()) -> list[tuple]:
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        conn.close()


def _scalar(sql: str, params: tuple = ()):
    rows = _query(sql, params)
    return rows[0][0] if rows else None


def _alembic(*args: str) -> None:
    """跑一次 alembic 命令 —— 与人工执行完全同一条路径（不绕 env.py）。"""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,  # 返回码由下面断言 —— 要让失败信息完整回显，而不是抛 CalledProcessError
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} 失败：\n{result.stdout}\n{result.stderr}"


def _indexes() -> dict[tuple[str, str], tuple[str, int]]:
    """全部索引 → ``{(表, 索引名): (列顺序, non_unique)}``。

    ⚠️ 键**必须带表名**：``PRIMARY`` 在每张表上都叫这个名字，只按索引名分组会把
    所有主键糊成一条毫无意义的记录（本测试第一版就是这么错的）。
    """
    rows = _query(
        "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, "
        "       GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
        "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE"
    )
    return {(table, name): (columns, non_unique) for table, name, non_unique, columns in rows}


@pytest.fixture(autouse=True)
def _at_head() -> Iterator[None]:
    """每个用例前后都确保库在 head —— 用例失败也不能把库留在 baseline。"""
    _alembic("upgrade", "head")
    yield
    _alembic("upgrade", "head")


# --------------------------------------------------------------------------- #
# 1：upgrade —— 5 个索引存在、列正确、是普通索引
# --------------------------------------------------------------------------- #
def test_the_five_query_indexes_exist_on_the_right_tables() -> None:
    indexes = _indexes()

    for name, (table, columns) in EXPECTED_INDEXES.items():
        assert (table, name) in indexes, f"{table}.{name} 不存在"
        assert indexes[(table, name)][0] == columns, f"{name} 的列顺序不对"


def test_the_query_indexes_are_ordinary_not_unique() -> None:
    """它们只是查询优化 —— 不能变成唯一约束，那会改变业务语义。"""
    indexes = _indexes()

    for name, (table, _) in EXPECTED_INDEXES.items():
        assert indexes[(table, name)][1] == 1, f"{name} 应当是普通索引（non_unique=1）"


def test_the_indexes_do_not_duplicate_existing_ones() -> None:
    """不与**同一张表上**的既有索引重复 —— 重复的索引只会拖慢写入。"""
    indexed = _indexes()
    mine_names = {(table, name) for name, (table, _) in EXPECTED_INDEXES.items()}
    mine_columns = {(table, columns) for table, columns in EXPECTED_INDEXES.values()}

    duplicates = [
        (table, name, columns)
        for (table, name), (columns, _) in indexed.items()
        if (table, name) not in mine_names and (table, columns) in mine_columns
    ]

    assert duplicates == [], f"存在与本次索引同表同列的既有索引：{duplicates}"


def test_every_foreign_key_column_still_has_an_index() -> None:
    """**结构性不变量**：每个外键列必须有索引可用（MySQL 的硬性要求）。

    本次迁移会接管 4 个外键原本依赖的单列索引。若接管之后又发生了别的变化，
    这条断言会立刻发现"某个外键失去了索引"——那会让后续写入直接报错。
    """
    rows = _query(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL"
    )
    indexed = _indexes()
    first_columns = {(table, columns.split(",")[0]) for (table, _), (columns, _) in indexed.items()}

    for table, column in rows:
        assert (table, column) in first_columns, f"{table}.{column} 是外键但没有以其为首的索引"


# --------------------------------------------------------------------------- #
# 2：downgrade —— 索引消失、外键索引回来
# --------------------------------------------------------------------------- #
def test_downgrade_removes_the_indexes_and_restores_the_foreign_key_indexes() -> None:
    """回滚必须**干净且可用**：删掉 5 个新索引，同时把外键的索引还回去。

    最后一步不是多余的 —— 不还的话 ``DROP`` 会被 MySQL 拒绝
    （1553：needed in a foreign key constraint），而且会在删掉前几个之后才失败，
    把库留在半吊子状态。
    """
    _alembic("downgrade", "-1")

    indexes = _indexes()
    still_there = [name for name, (table, _) in EXPECTED_INDEXES.items() if (table, name) in indexes]
    assert still_there == [], "5 个索引应当全部消失"
    assert _scalar("SELECT version_num FROM alembic_version") == "313b0960b510"

    for table, fk_index in FK_INDEXES.items():
        assert (table, fk_index) in indexes, f"{table} 的外键索引 {fk_index} 没有还回来"

    # 外键本身当然还在
    rows = _query(
        "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
        "WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_TYPE = 'FOREIGN KEY'"
    )
    names = {row[0] for row in rows}
    for fk_index in FK_INDEXES.values():
        assert fk_index in names, f"外键 {fk_index} 不该被回滚删掉"


# --------------------------------------------------------------------------- #
# 3：往返 —— upgrade 与库的历史路径无关
# --------------------------------------------------------------------------- #
def test_upgrade_lands_in_the_same_state_after_a_round_trip() -> None:
    """先 downgrade 再 upgrade，最终结构必须与"干净升级"**逐索引一致**。

    这条守的是一个具体的坑：升级会**接管**外键自己的单列索引，而 MySQL 只肯自动
    接管**它自己创建**的那一份。回滚时那份是显式建的，再升级时 MySQL 就不动了 ——
    于是同一列上会留下两个索引，`upgrade` 的结果取决于"库是怎么走到这一步的"。
    """
    before = _indexes()

    _alembic("downgrade", "-1")
    _alembic("upgrade", "head")

    assert _indexes() == before, "往返之后的索引集合与干净升级不一致"


def test_a_round_trip_leaves_no_redundant_foreign_key_index() -> None:
    _alembic("downgrade", "-1")
    _alembic("upgrade", "head")

    indexes = _indexes()
    leftovers = [fk_index for table, fk_index in FK_INDEXES.items() if (table, fk_index) in indexes]

    assert leftovers == [], f"升级后仍留着重复的外键索引：{leftovers}"
