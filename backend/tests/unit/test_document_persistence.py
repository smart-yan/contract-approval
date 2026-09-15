"""文档层持久化的单元测试（**不需要 MySQL**）。

这里只测在数据库之外就能钉死的事：

1. **请求契约**：哪些字段必填、块的引用怎么校验、`parse_status` 接受哪些取值
2. **两个纯判据**：定位方式派生、解析状态迁移规则

真正的落库行为（事务、文件级复用、并发、回滚）在
``tests/integration/test_document_api.py`` 里，需要真实 MySQL。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.constants import LocatorType, ParseStatus
from app.db.models.contract import ContractFile
from app.schemas.document import (
    ClauseCreate,
    DocumentBlockCreate,
    DocumentPersistRequest,
    MetadataItemCreate,
)
from app.services.document_persistence import (
    _assert_parse_status_transition,
    _locator_type_of,
)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _block(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "order_index": 0,
        "paragraph_index": 0,
        "block_type": "PARAGRAPH",
        "text": "第一条 知识产权",
        "char_start_global": 0,
        "char_end_global": 9,
    }
    payload.update(overrides)
    return payload


def _clause(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "clause_type": "IP",
        "text": "第一条 知识产权",
        "extract_method": "RULE",
        "start_block_index": 0,
        "end_block_index": 0,
    }
    payload.update(overrides)
    return payload


def _request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "parse_status": "PARSED",
        "blocks": [_block()],
        "clauses": [_clause()],
        "metadata": [],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# 请求契约
# --------------------------------------------------------------------------- #
def test_a_minimal_request_is_valid() -> None:
    request = DocumentPersistRequest(**_request())

    assert request.parse_status == "PARSED"
    assert len(request.blocks) == 1
    assert request.clauses[0].clause_type.value == "IP"


@pytest.mark.parametrize("bad", ["PENDING", "PARSING", "DONE", ""])
def test_only_terminal_parse_statuses_are_accepted(bad: str) -> None:
    """``PENDING`` 是上传时的初始值，``PARSING`` 在同步编排里不可观测 ——
    都不该由调用方指定，因此拒收。"""
    with pytest.raises(ValidationError):
        DocumentPersistRequest(**_request(parse_status=bad))


def test_an_empty_document_is_allowed() -> None:
    """空文档（``PARSED`` + 0 块）是**正常结论**，不是错误。"""
    request = DocumentPersistRequest(**_request(blocks=[], clauses=[]))

    assert request.blocks == []


def test_a_failed_parse_carries_nothing() -> None:
    request = DocumentPersistRequest(**_request(parse_status="FAILED", blocks=[], clauses=[]))

    assert request.parse_status == "FAILED"


# ---- 块引用 ----
@pytest.mark.parametrize("field", ["start_block_index", "end_block_index"])
def test_a_clause_reference_out_of_range_is_rejected(field: str) -> None:
    """越界的引用是**调用方的错误** —— 放过去就会写出指向无关块的条款。"""
    with pytest.raises(ValidationError):
        DocumentPersistRequest(**_request(clauses=[_clause(**{field: 5})]))


def test_a_backwards_clause_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DocumentPersistRequest(
            **_request(
                blocks=[_block(order_index=0), _block(order_index=1, paragraph_index=1)],
                clauses=[_clause(start_block_index=1, end_block_index=0)],
            )
        )


def test_a_metadata_reference_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DocumentPersistRequest(
            **_request(
                metadata=[
                    {
                        "field_key": "k",
                        "field_label": "l",
                        "field_value": "v",
                        "value_type": "TEXT",
                        "extract_method": "REGEX",
                        "source_block_index": 3,
                    }
                ]
            )
        )


def test_a_metadata_without_a_source_block_is_allowed() -> None:
    """抽取失败时没有来源块 —— 这是合法状态（``source_block_id`` 可空）。"""
    request = DocumentPersistRequest(
        **_request(
            metadata=[
                {
                    "field_key": "k",
                    "field_label": "l",
                    "field_value": "v",
                    "value_type": "TEXT",
                    "extract_method": "REGEX",
                }
            ]
        )
    )

    assert request.metadata[0].source_block_index is None


def test_duplicate_order_index_is_rejected() -> None:
    """文件级复用要靠 ``order_index`` 建一一对应，重复了就没有确定的映射。"""
    with pytest.raises(ValidationError):
        DocumentPersistRequest(
            **_request(blocks=[_block(order_index=0), _block(order_index=0, paragraph_index=1)])
        )


# ---- 块自身的字段 ----
def test_the_block_required_fields_match_the_orm() -> None:
    """请求**完整接收** ORM 必需字段；只有三处"当前无来源"的可以省略。"""
    assert set(DocumentBlockCreate.model_fields) == {
        "order_index",
        "paragraph_index",
        "block_type",
        "text",
        "char_start_global",
        "char_end_global",
        "raw_text",
        "char_start_in_block",
        "char_end_in_block",
        "page_number",
        "bbox_json",
        "ocr_confidence",
    }


def test_a_backwards_global_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DocumentBlockCreate(**_block(char_start_global=100, char_end_global=50))


def test_the_three_sourceless_fields_may_be_omitted() -> None:
    """``raw_text`` / ``char_*_in_block`` 当前没有真实来源（Parser 未保留归一化前原文）
    —— 省略是正常用法，由服务端按文档口径补齐。"""
    block = DocumentBlockCreate(**_block())

    assert block.raw_text is None
    assert block.char_start_in_block is None
    assert block.char_end_in_block is None


@pytest.mark.parametrize("value", ["PARAGRAPH", "TABLE_ROW", "TITLE", "HEADER", "FOOTER"])
def test_every_block_type_the_orm_declares_is_accepted(value: str) -> None:
    """取值集合**直接用 ORM 的枚举**，不在这里另立一套。

    ⚠️ Agent 目前只产 ``PARAGRAPH`` / ``TABLE_ROW``；``TITLE`` / ``HEADER`` / ``FOOTER``
    是合法取值但永远不会出现（§7.2 的 ``document_block.block_type`` 是同一份枚举）。
    收窄成"只有 Agent 会产的两种"会让将来接入 PDF/OCR 时这里先炸。
    """
    assert DocumentBlockCreate(**_block(block_type=value)).block_type.value == value


def test_an_unknown_block_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DocumentBlockCreate(**_block(block_type="OTHER"))


def test_clause_fields_match_the_orm() -> None:
    """``clause`` 表**没有**段落号列 —— 请求里也不该出现它们。"""
    assert set(ClauseCreate.model_fields) == {
        "clause_type",
        "text",
        "extract_method",
        "clause_no",
        "title",
        "start_block_index",
        "end_block_index",
        "char_start_global",
        "char_end_global",
        "page_start",
        "page_end",
        "confidence",
    }
    assert "start_paragraph_index" not in ClauseCreate.model_fields
    assert "end_paragraph_index" not in ClauseCreate.model_fields


def test_value_type_is_not_a_closed_set() -> None:
    """``value_type`` 在项目里**没有枚举**（§7.2 只给了列注释），
    因此按自由文本接收 —— 在这里造一个闭集等于凭空新增一套词表。"""
    item = MetadataItemCreate(
        field_key="k",
        field_label="l",
        field_value="v",
        value_type="SOMETHING_NEW",
        extract_method="REGEX",
    )

    assert item.value_type == "SOMETHING_NEW"


def test_value_type_respects_the_column_width() -> None:
    with pytest.raises(ValidationError):
        MetadataItemCreate(
            field_key="k",
            field_label="l",
            field_value="v",
            value_type="X" * 17,
            extract_method="REGEX",
        )


# --------------------------------------------------------------------------- #
# 定位方式派生（与风险层同一口径）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("file_ext", "expected"),
    [
        ("docx", LocatorType.PARAGRAPH.value),
        (".DOCX", LocatorType.PARAGRAPH.value),
        ("pdf", LocatorType.PAGE.value),
        ("png", LocatorType.PAGE.value),
    ],
)
def test_the_locator_type_is_derived_from_the_extension(file_ext: str, expected: str) -> None:
    assert _locator_type_of(file_ext) == expected


def test_the_locator_rule_is_shared_with_the_risk_layer() -> None:
    """两处必须**同一份实现**：`document_block.locator_type` 与
    `risk_item.locator_type` 一旦不同，前端按它选的定位文案就会与坐标矛盾。"""
    import app.services.document_persistence as documents
    import app.services.risk_persistence as risks

    assert documents._normalize_ext is risks._normalize_ext
    assert documents._PARAGRAPH_LOCATOR_TYPES is risks._PARAGRAPH_LOCATOR_TYPES


# --------------------------------------------------------------------------- #
# 解析状态迁移
# --------------------------------------------------------------------------- #
def _file(status: str) -> ContractFile:
    return ContractFile(
        contract_id=1,
        file_name="c.docx",
        file_ext="docx",
        file_size=1,
        sha256="a" * 64,
        storage_path="x",
        is_scanned=False,
        parse_status=status,
    )


@pytest.mark.parametrize("target", [ParseStatus.PARSED.value, ParseStatus.FAILED.value])
def test_pending_may_transition_to_either_terminal(target: str) -> None:
    _assert_parse_status_transition(_file(ParseStatus.PENDING.value), target)


@pytest.mark.parametrize("status", [ParseStatus.PARSED.value, ParseStatus.FAILED.value])
def test_the_same_terminal_value_is_an_idempotent_noop(status: str) -> None:
    """同一文件服务多个审查任务时会**再次**提交同样的状态 —— 必须放行，
    否则文件复用这条已批准的能力就被门禁打死了。"""
    _assert_parse_status_transition(_file(status), status)


def test_a_failed_file_may_be_upgraded_by_a_later_task() -> None:
    """``FAILED`` **不是**文件级永久终态 —— 它只是"某一次尝试失败了"。

    失败时一个块都没有落库，因此后续任务重新解析成功时把状态升级为 ``PARSED``
    是正确的（这才是文件第一次真正拥有 ``document_block``）。禁止它会让
    "解析器修好了"之后**永远无法持久化**，而库里还什么都没有。
    """
    _assert_parse_status_transition(_file(ParseStatus.FAILED.value), ParseStatus.PARSED.value)


def test_a_parsed_file_may_not_be_downgraded() -> None:
    """``PARSED`` 才是不许覆盖的那一侧：它的 ``document_block`` 已经落库并被复用，
    改成 ``FAILED`` 会留下"状态说失败、库里躺着整套段落"的自相矛盾。"""
    from app.core.errors import ConflictError, ErrorCode

    with pytest.raises(ConflictError) as excinfo:
        _assert_parse_status_transition(_file(ParseStatus.PARSED.value), ParseStatus.FAILED.value)

    assert excinfo.value.code is ErrorCode.PARSE_STATUS_ALREADY_FINAL
    assert excinfo.value.http_status == 409


def test_only_one_transition_is_forbidden() -> None:
    """把整张迁移表钉死：**唯一**被禁止的就是 ``PARSED → FAILED``。

    这条用例的价值在于"将来有人再收紧规则时，会有东西报错"——
    回归到"两个终态互不可达"会让失败文件永远无法重试。
    """
    from app.core.errors import ConflictError

    allowed = [
        (ParseStatus.PENDING.value, ParseStatus.PARSED.value),
        (ParseStatus.PENDING.value, ParseStatus.FAILED.value),
        (ParseStatus.FAILED.value, ParseStatus.PARSED.value),
        (ParseStatus.FAILED.value, ParseStatus.FAILED.value),
        (ParseStatus.PARSED.value, ParseStatus.PARSED.value),
    ]

    for current, target in allowed:
        _assert_parse_status_transition(_file(current), target)

    with pytest.raises(ConflictError):
        _assert_parse_status_transition(_file(ParseStatus.PARSED.value), ParseStatus.FAILED.value)
