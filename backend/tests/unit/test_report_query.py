"""报告查询层的单元测试（**不需要 MySQL**）。

真正的查询（4 条 SELECT、404/409 的端到端行为、真实 MySQL 上的隔离）留在
P12-3 的集成测试里 —— 与 ``test_contract_query.py`` / ``test_risk_persistence.py``
的分工一致：单元测试只钉**纯函数**与**契约**。

这里钉两件在数据库之外就能定死的事：

1. **阶段门禁** —— 只有 ``REVIEWED`` 能出报告，且失败必须是
   ``REPORT_NOT_READY`` / 409（**409 而不是 404**：任务存在，只是还不能出报告）
2. **ORM 行 → ReportData 的装配** —— 字段有没有搬错、空值有没有被"顺手补上"
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.constants import TaskStage
from app.core.errors import ERROR_SPECS, ConflictError, ErrorCode
from app.services.report_query import _assemble, _ensure_reportable

TASK_ID = 42


def naive_utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """构造一个**固定**的 naive-UTC 时刻（与 ``app.utils.datetime_utils.utcnow()`` 同口径）。

    项目写库一律 naive UTC（MySQL 的 DATETIME 不存时区）。因此先取 aware 再摘掉
    tzinfo，而不是写一个看起来像本地时间的字面量 —— ruff 的 DTZ001 拦下后者是对的。
    """
    return datetime(year, month, day, hour, minute, tzinfo=UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# 造行（SimpleNamespace 冒充 ORM 行：装配是纯函数，只按属性取值）
# --------------------------------------------------------------------------- #
def _row(defaults: dict[str, Any], overrides: dict[str, Any]) -> SimpleNamespace:
    """造一行：先铺默认值再覆盖 —— 这样 ``id=...`` 之类的覆盖才不会重复传参。"""
    return SimpleNamespace(**{**defaults, **overrides})


def make_task(**overrides: Any) -> SimpleNamespace:
    return _row(
        {
            "id": TASK_ID,
            "status": "pending",
            "current_stage": TaskStage.REVIEWED.value,
            "created_at": naive_utc(2026, 9, 15, 10, 0),
        },
        overrides,
    )


def make_contract(**overrides: Any) -> SimpleNamespace:
    return _row(
        {
            "id": 1,
            "contract_no": "HT-2026-001",
            "title": "设备采购合同",
            "contract_type": "PURCHASE",
            "our_party": "某某科技",
            "counterparty": "乙方公司",
            "amount": Decimal("1234.50"),
            "currency": "CNY",
            "sign_date": date(2026, 9, 15),
            "effective_date": date(2026, 10, 1),
            "expire_date": None,
            "dept": "法务部",
        },
        overrides,
    )


def make_file(**overrides: Any) -> SimpleNamespace:
    return _row(
        {
            "id": 7,
            "file_name": "采购合同-风险版.docx",
            "file_ext": "docx",
            "sha256": "a" * 64,
            "parse_status": "PARSED",
        },
        overrides,
    )


def make_risk_row(**overrides: Any) -> SimpleNamespace:
    return _row(
        {
            "id": 900,
            "risk_code": "IP_OWNER_SUPPLIER_001",
            "risk_title": "知识产权归属相对方",
            "dimension": "知识产权",
            "risk_level": "HIGH",
            "source": "RULE",
            "reason": "成果归属供方会限制我方后续使用。",
            "legal_basis": "《民法典》第八百四十七条",
            "original_text": "知识产权归乙方",
            "paragraph_index": 23,
            "clause_id": 100,
            "locator_type": "PARAGRAPH",
            "review_status": "PENDING",
            # 人工复核的补充两列（P13-4）。未复核的历史数据就是这两个 NULL
            "review_comment": None,
            "reviewed_at": None,
        },
        overrides,
    )


def make_clause_row(**overrides: Any) -> SimpleNamespace:
    return _row(
        {
            "id": 100,
            "clause_no": "第三条",
            "clause_type": "IP",
            "title": "知识产权",
            "text": "第三条 知识产权……",
        },
        overrides,
    )


def make_metadata_row(**overrides: Any) -> SimpleNamespace:
    return _row(
        {
            "field_key": "counterparty_name",
            "field_label": "相对方名称",
            "field_value": "乙方公司",
            "value_type": "TEXT",
            "extract_method": "REGEX",
            # 下面这些列**不**进报告：source_block_id 要经 document_block 换算段落号，
            # 而报告不查 block（见 report_render 的说明）
            "source_block_id": 2,
        },
        overrides,
    )


def assemble(**overrides: Any):
    kwargs: dict[str, Any] = {
        "task": make_task(),
        "contract": make_contract(),
        "contract_file": make_file(),
        "clauses": [make_clause_row()],
        "metadata": [make_metadata_row()],
        "risks": [make_risk_row()],
    }
    kwargs.update(overrides)
    return _assemble(**kwargs)


# --------------------------------------------------------------------------- #
# 1：阶段门禁
# --------------------------------------------------------------------------- #
def test_a_reviewed_task_is_reportable() -> None:
    assert _ensure_reportable(TASK_ID, TaskStage.REVIEWED.value) is None


@pytest.mark.parametrize(
    "stage",
    [TaskStage.UPLOADED.value, TaskStage.PARSED.value, TaskStage.CLAUSED.value],
)
def test_an_unfinished_task_is_refused(stage: str) -> None:
    """``CLAUSED`` 尤其关键：那时文档层已落库、**风险还没写**。

    此时出报告会得到一份**零风险**的报告 —— 读者会把"还没审完"读成"没有风险"。
    """
    with pytest.raises(ConflictError) as excinfo:
        _ensure_reportable(TASK_ID, stage)

    assert excinfo.value.code is ErrorCode.REPORT_NOT_READY
    assert excinfo.value.details == {"task_id": TASK_ID, "current_stage": stage}


def test_an_unknown_stage_is_refused_too() -> None:
    """不认识的阶段一律拒绝 —— 白名单，不是黑名单。

    黑名单（"只要不是 UPLOADED 就放行"）在以后新增阶段时会静默放行一个
    谁都没想清楚的状态。
    """
    with pytest.raises(ConflictError):
        _ensure_reportable(TASK_ID, "SOMETHING_NEW")


def test_report_not_ready_is_a_409_and_not_a_404() -> None:
    """任务**存在**，只是还不能出报告。

    用 404 会让前端把"还在审查中"显示成"任务不存在"，把用户引向错误的方向。
    """
    spec = ERROR_SPECS[ErrorCode.REPORT_NOT_READY]

    assert spec.http_status == 409
    with pytest.raises(ConflictError):
        _ensure_reportable(TASK_ID, TaskStage.UPLOADED.value)


def test_the_gate_reuses_an_existing_error_code() -> None:
    """不新增错误码：``REPORT_NOT_READY`` 早就在 ``errors.py`` 里登记好了。"""
    assert ErrorCode.REPORT_NOT_READY.value == "REPORT_NOT_READY"


# --------------------------------------------------------------------------- #
# 2：ORM 行 → ReportData
# --------------------------------------------------------------------------- #
def test_the_task_is_mapped_without_the_null_columns() -> None:
    data = assemble()

    assert data.task.task_id == TASK_ID
    assert data.task.current_stage == TaskStage.REVIEWED.value
    assert data.task.created_at == naive_utc(2026, 9, 15, 10, 0)
    # 恒为 NULL / 属评分结果的列**不**进模型（见 report_render.ReportTask）
    for absent in ("finished_at", "risk_level_final", "conclusion"):
        assert not hasattr(data.task, absent)


def test_the_contract_and_file_are_mapped_column_by_column() -> None:
    data = assemble()

    assert data.contract.contract_no == "HT-2026-001"
    assert data.contract.amount == Decimal("1234.50")
    assert data.contract.expire_date is None
    assert data.file.file_name == "采购合同-风险版.docx"
    assert data.file.sha256 == "a" * 64


def test_the_file_type_is_derived_from_the_extension() -> None:
    """表里只有 ``file_ext``，类型码由**上传时用的同一个函数**还原，口径不会漂。"""
    assert assemble().file.file_type == "DOCX"
    assert assemble(contract_file=make_file(file_ext=".PDF")).file.file_type == "PDF"


def test_an_unrecognised_extension_falls_back_to_the_raw_value() -> None:
    data = assemble(contract_file=make_file(file_ext="xyz"))

    assert data.file.file_type == "xyz"


@pytest.mark.parametrize(
    "field",
    [
        "risk_code",
        "risk_title",
        "dimension",
        "risk_level",
        "source",
        "reason",
        "legal_basis",
        "original_text",
        "paragraph_index",
        "clause_id",
        "locator_type",
        "review_status",
    ],
)
def test_every_risk_field_survives_the_mapping(field: str) -> None:
    """风险是报告的主体，少搬一列就会让报告少一块内容。"""
    row = make_risk_row()
    data = assemble(risks=[row])

    assert getattr(data.risks[0], field) == getattr(row, field)


def test_a_nullable_risk_field_is_not_filled_in() -> None:
    """``reason`` / ``legal_basis`` / ``original_text`` / ``clause_id`` 可以为空。

    装配层**原样搬**，不写占位符 —— "这条风险没说原因"和"原因是'无'"是两件事。
    """
    data = assemble(
        risks=[make_risk_row(reason=None, legal_basis=None, original_text=None, clause_id=None)]
    )

    risk = data.risks[0]
    assert risk.reason is None
    assert risk.legal_basis is None
    assert risk.original_text is None
    assert risk.clause_id is None


def test_the_review_columns_are_mapped_with_real_values() -> None:
    """P13-4：人工复核三列要原样搬到 ``ReportRisk``。

    ⚠️ 用**非空**值断言 —— 默认行里这三列是 NULL，``None == None`` 会让
    "漏搬一列"这种错误照样通过。
    """
    row = make_risk_row(
        review_status="MODIFIED",
        review_comment="等级下调",
        reviewed_at=naive_utc(2026, 9, 16, 8, 12),
    )

    risk = assemble(risks=[row]).risks[0]

    assert risk.review_status == "MODIFIED"
    assert risk.review_comment == "等级下调"
    assert risk.reviewed_at == naive_utc(2026, 9, 16, 8, 12)


def test_an_unreviewed_risk_maps_to_nulls_not_placeholders() -> None:
    """未复核的历史数据原样搬 NULL —— 不写"无"、不写空串。"""
    risk = assemble().risks[0]

    assert risk.review_status == "PENDING"
    assert risk.review_comment is None
    assert risk.reviewed_at is None


def test_metadata_keeps_only_the_display_columns() -> None:
    data = assemble()

    assert data.metadata[0].field_label == "相对方名称"
    assert data.metadata[0].field_value == "乙方公司"
    # source_block_id 不进报告：它要经 document_block 换算，而报告不查 block
    assert not hasattr(data.metadata[0], "source_block_id")


def test_empty_collections_become_empty_tuples() -> None:
    data = assemble(clauses=[], metadata=[], risks=[])

    assert data.clauses == ()
    assert data.metadata == ()
    assert data.risks == ()


def test_the_order_of_each_collection_is_preserved() -> None:
    """查询层按 ``id`` 升序取；装配层不许重排（排序口径属展示决策）。"""
    data = assemble(
        risks=[make_risk_row(id=900 + i) for i in range(3)],
        clauses=[make_clause_row(id=100 + i) for i in range(3)],
    )

    assert [risk.risk_id for risk in data.risks] == [900, 901, 902]
    assert [clause.clause_id for clause in data.clauses] == [100, 101, 102]
