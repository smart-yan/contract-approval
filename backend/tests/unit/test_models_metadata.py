"""P3 ORM 模型的**元数据级**测试（不需要数据库）。

这些断言在生成迁移**之前**就能发现问题：表集合、列类型、约束、命名规范、
以及一系列"刻意为之"的设计决定（不建 FK、不建 relationship、不用 server_default）。

真实数据库层面的核对在 ``tests/integration/test_models_schema.py``。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.mysql import DATETIME, MEDIUMTEXT, TINYINT
from sqlalchemy.sql.schema import UniqueConstraint

from app.core import constants as C
from app.db import models  # noqa: F401  触发模型注册
from app.db.base import Base

# --------------------------------------------------------------------------- #
# 期望的表集合
# --------------------------------------------------------------------------- #
EXPECTED_TABLES: set[str] = {
    "contract",
    "contract_file",
    "review_task",
    "document_block",
    "contract_metadata",
    "clause",
    "review_rule_set",
    "review_rule",
    "risk_item",
    "risk_suggestion",
    "writeback_record",
    "ai_call_log",
    "approval_instance",
    "approval_comment",
    "sync_cursor",
}

#: 字段定义不完整、经架构裁决**刻意延后**的表。它们一旦出现就说明有人提前建表了。
DEFERRED_TABLES: set[str] = {
    "sys_user",  # P4：§7.2 无字段定义
    "task_event",  # P6：§7.2 无字段定义
    "annotation",  # P11：§7.2 无字段定义
    "standard_clause",  # P13：§7.2 无字段定义
    "report",  # P12：§7.2 无字段定义
    "writeback_log",  # P12：§7.1 仅提及，无字段定义
}

#: 刻意**不建 FK** 的列（目标表尚未创建，或会形成循环外键）。
DELIBERATELY_FK_FREE: set[tuple[str, str]] = {
    ("contract", "applicant_id"),  # sys_user 属 P4
    ("contract", "current_task_id"),  # 与 review_task.contract_id 循环
    ("contract", "approval_instance_id"),  # §7.2 未标注为 FK
    ("risk_suggestion", "standard_clause_id"),  # standard_clause 属 P13
}


# --------------------------------------------------------------------------- #
# 表集合
# --------------------------------------------------------------------------- #
def test_exactly_the_fifteen_approved_tables_exist() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


@pytest.mark.parametrize("table_name", sorted(DEFERRED_TABLES))
def test_deferred_tables_are_not_created(table_name: str) -> None:
    assert table_name not in Base.metadata.tables, f"{table_name} 在 §7.2 中没有字段定义，应延后到对应阶段"


# --------------------------------------------------------------------------- #
# 公共约定
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_every_table_has_base_mixin_columns(table_name: str) -> None:
    cols = Base.metadata.tables[table_name].columns
    assert "id" in cols and cols["id"].primary_key
    assert "created_at" in cols and not cols["created_at"].nullable
    assert "updated_at" in cols and not cols["updated_at"].nullable


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_all_datetime_columns_use_millisecond_precision(table_name: str) -> None:
    """§7.2 约定：时间列一律 DATETIME(3)。"""
    for col in Base.metadata.tables[table_name].columns:
        if isinstance(col.type, DATETIME):
            assert col.type.fsp == 3, f"{table_name}.{col.name} 缺少 fsp=3"


def test_no_mysql_native_enum_is_used() -> None:
    """建模纪律：枚举字段一律 VARCHAR 存字面值，不用 MySQL 原生 ENUM。"""
    offenders = [
        f"{t.name}.{c.name}"
        for t in Base.metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, SAEnum)
    ]
    assert not offenders, f"禁止使用原生 ENUM：{offenders}"


def test_no_server_default_anywhere() -> None:
    """决策 D4：时间由 Python 侧 utcnow 负责，不加 server_default。"""
    offenders = [
        f"{t.name}.{c.name}"
        for t in Base.metadata.tables.values()
        for c in t.columns
        if c.server_default is not None
    ]
    assert not offenders, f"不应存在 server_default：{offenders}"


def test_no_orm_relationships_defined() -> None:
    """决策 I：P3 只建模，不建立数据访问语义（relationship / cascade）。"""
    total = sum(len(m.relationships) for m in Base.registry.mappers)
    assert total == 0, f"P3 不应定义任何 relationship，当前有 {total} 个"


def test_all_foreign_keys_target_existing_tables() -> None:
    """防止悬空外键 —— 指向未创建表的 FK 会让迁移在 DDL 阶段直接失败。"""
    dangling = [
        f"{t.name}.{fk.parent.name} -> {fk.column.table.name}"
        for t in Base.metadata.tables.values()
        for fk in t.foreign_keys
        if fk.column.table.name not in Base.metadata.tables
    ]
    assert not dangling, f"悬空外键：{dangling}"


def test_no_foreign_key_declares_cascade_behaviour() -> None:
    """决策 O6：FK 一律保持 MySQL 默认（RESTRICT / NO ACTION），禁止级联。"""
    offenders = [
        f"{t.name}.{fk.parent.name}(ondelete={fk.ondelete}, onupdate={fk.onupdate})"
        for t in Base.metadata.tables.values()
        for fk in t.foreign_keys
        if fk.ondelete is not None or fk.onupdate is not None
    ]
    assert not offenders, f"禁止声明 ondelete/onupdate：{offenders}"


@pytest.mark.parametrize(("table_name", "column_name"), sorted(DELIBERATELY_FK_FREE))
def test_deliberately_fk_free_columns(table_name: str, column_name: str) -> None:
    col = Base.metadata.tables[table_name].columns[column_name]
    assert not col.foreign_keys, f"{table_name}.{column_name} 刻意不建 FK"


# --------------------------------------------------------------------------- #
# 金额与精度
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [("contract", "amount"), ("approval_instance", "amount")],
)
def test_amount_columns_are_decimal_18_2(table_name: str, column_name: str) -> None:
    """§7.2 约定：金额一律 DECIMAL(18,2)，禁止 float。"""
    col = Base.metadata.tables[table_name].columns[column_name]
    assert col.type.__class__.__name__ == "Numeric"
    assert (col.type.precision, col.type.scale) == (18, 2)


def test_float_is_only_used_for_confidence_scores() -> None:
    """Float 只允许出现在置信度类字段上。"""
    allowed = {
        ("document_block", "ocr_confidence"),
        ("contract_metadata", "confidence"),
        ("clause", "confidence"),
        ("risk_item", "anchor_score"),
        ("risk_item", "confidence"),
    }
    actual = {
        (t.name, c.name)
        for t in Base.metadata.tables.values()
        for c in t.columns
        if c.type.__class__.__name__ == "Float"
    }
    assert actual == allowed, f"Float 用在了非置信度字段：{actual - allowed}"


# --------------------------------------------------------------------------- #
# 唯一约束
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("contract", "contract_no"),
        ("contract_file", "sha256"),
        ("review_task", "idempotency_key"),
        ("writeback_record", "idempotency_key"),
    ],
)
def test_key_unique_columns_are_unique_and_not_null(table_name: str, column_name: str) -> None:
    """幂等键/业务键必须 NOT NULL UNIQUE —— 允许 NULL 会让 UNIQUE 约束被静默绕过。"""
    col = Base.metadata.tables[table_name].columns[column_name]
    assert col.unique is True
    assert col.nullable is False


def test_approval_comment_idempotency_key_is_scoped_to_the_instance() -> None:
    """P15-3a：幂等键是"**外部请求**的身份"，只在**审批单内**唯一。

    ⚠️ 列**可空**是刻意的：人工评论（``source=MANUAL``）没有外部请求身份，
    硬要它编一个键就是伪造。MySQL 的唯一索引不把多个 NULL 视为冲突，
    因此可空列在本约束下仍允许任意多条人工评论。
    """
    table = Base.metadata.tables["approval_comment"]
    composites = {
        frozenset(c.name for c in uc.columns) for uc in table.constraints if isinstance(uc, UniqueConstraint)
    }
    assert frozenset({"instance_id", "idempotency_key"}) in composites

    column = table.columns["idempotency_key"]
    assert column.nullable is True, "人工评论没有幂等键，列必须可空"
    assert column.unique is None, "幂等键不是全局唯一 —— 两个审批单可以各自收到同一个键"


def test_review_rule_has_composite_unique_within_set() -> None:
    """§7.2「rule_code UNIQUE within set」→ 复合唯一，而非全局唯一。"""
    table = Base.metadata.tables["review_rule"]
    composites = {
        frozenset(c.name for c in uc.columns) for uc in table.constraints if isinstance(uc, UniqueConstraint)
    }
    assert frozenset({"rule_set_id", "rule_code"}) in composites
    assert table.columns["rule_code"].unique is None, "rule_code 不应是全局唯一"


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
def test_review_task_indexes_keep_documented_names() -> None:
    """§7.2 明确给出了这两个索引名，必须逐字一致。"""
    names = {i.name for i in Base.metadata.tables["review_task"].indexes}
    assert {"idx_claim", "idx_heartbeat"} <= names

    claim = next(i for i in Base.metadata.tables["review_task"].indexes if i.name == "idx_claim")
    assert [c.name for c in claim.columns] == ["status", "next_retry_at", "priority", "id"]


def test_document_block_indexes_follow_naming_convention() -> None:
    names = {i.name for i in Base.metadata.tables["document_block"].indexes}
    assert names == {
        "ix_document_block_contract_id_order_index",
        "ix_document_block_contract_id_char_start_global",
    }


# --------------------------------------------------------------------------- #
# 关键枚举字段：列宽必须容得下枚举字面值
# --------------------------------------------------------------------------- #
ENUM_COLUMNS: list[tuple[str, str, type]] = [
    ("contract", "contract_type", C.ContractType),
    ("contract", "source", C.ContractSource),
    ("contract", "status", C.TaskStatus),
    ("review_task", "status", C.TaskStatus),
    ("review_task", "current_stage", C.TaskStage),
    ("review_task", "block_reason_code", C.BlockReasonCode),
    ("review_task", "risk_level_final", C.RiskLevel),
    ("review_task", "conclusion", C.ReviewConclusion),
    ("document_block", "block_type", C.BlockType),
    ("document_block", "locator_type", C.LocatorType),
    ("contract_metadata", "extract_method", C.ExtractMethod),
    ("clause", "clause_type", C.ClauseType),
    ("clause", "extract_method", C.ExtractMethod),
    ("review_rule", "rule_type", C.RuleType),
    ("review_rule", "severity", C.RiskLevel),
    ("risk_item", "risk_level", C.RiskLevel),
    ("risk_item", "source", C.RiskSource),
    ("risk_item", "locator_type", C.LocatorType),
    ("risk_item", "anchor_method", C.AnchorMethod),
    ("risk_item", "review_status", C.RiskReviewStatus),
    ("risk_suggestion", "suggestion_type", C.SuggestionType),
    ("writeback_record", "status", C.WritebackStatus),
    ("ai_call_log", "scene", C.LLMScene),
]


@pytest.mark.parametrize(
    ("table_name", "column_name", "enum_cls"),
    ENUM_COLUMNS,
    ids=[f"{t}.{c}" for t, c, _ in ENUM_COLUMNS],
)
def test_enum_column_can_hold_every_enum_value(table_name: str, column_name: str, enum_cls: type) -> None:
    """枚举列的 VARCHAR 长度必须 >= 该枚举最长字面值。

    这条断言能在**建表之前**发现"字段长度明显不足"这类问题 ——
    否则等某个枚举加长时，插入会在运行期才报 Data too long。
    """
    col = Base.metadata.tables[table_name].columns[column_name]
    length = getattr(col.type, "length", None)
    assert length is not None, f"{table_name}.{column_name} 不是变长字符串列"

    longest = max(len(member.value) for member in enum_cls)
    assert length >= longest, (
        f"{table_name}.{column_name} 长度 {length} 装不下 {enum_cls.__name__} 的最长值（{longest} 字符）"
    )


def test_review_task_progress_is_tinyint() -> None:
    assert isinstance(Base.metadata.tables["review_task"].columns["progress"].type, TINYINT)


def test_ai_call_log_raw_response_is_mediumtext() -> None:
    """§7.2 明确指定 MEDIUMTEXT（模型原始响应可能很长）。"""
    assert isinstance(Base.metadata.tables["ai_call_log"].columns["raw_response"].type, MEDIUMTEXT)
