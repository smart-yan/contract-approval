"""文档层持久化的边界层（P10-3）。

这一层要钉死的是**坐标**，不是字段名：

* ``char_start_global`` / ``char_end_global`` 必须真的能切出那一段原文
* 条款的段落区间必须换算成 ``blocks[]`` 的下标，且**不允许错位**
* ``EMPTY`` 与 ``FAILED`` 不能混为一谈

因此核心断言是 ``text[start:end] == paragraph.text`` 这种**可验证的不变量**，
而不是"某个字段等于某个值"。
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from app.schemas.document import Paragraph, ParseResult
from app.schemas.understanding import Clause, MetadataItem
from app.tools.backend_client import BackendClient
from app.tools.document_persistence import (
    DocumentMappingError,
    DocumentPersistenceRequest,
    DocumentPersistenceTool,
)

BACKEND_BASE_URL = "http://backend.test"
EXPECTED_URL = f"{BACKEND_BASE_URL}/api/v1/review-tasks/33/document"

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]

#: 三段文本，第 2 段是表格行 —— 覆盖 ``PARAGRAPH`` 与 ``TABLE_ROW`` 两种块类型
_TEXTS = ("第一条 知识产权", "本项目产生的知识产权归乙方所有。", "第二条 违约责任")

_OK_BODY: dict[str, Any] = {
    "task_id": 33,
    "parse_status": "PARSED",
    "blocks_created": 3,
    "blocks_reused": 0,
    "clauses_persisted": 1,
    "metadata_persisted": 1,
    "current_stage": "CLAUSED",
}


def _parse_result(**overrides: Any) -> ParseResult:
    paragraphs = [
        Paragraph(index=i, block_type="TABLE_ROW" if i == 1 else "PARAGRAPH", text=text)
        for i, text in enumerate(_TEXTS)
    ]
    payload: dict[str, Any] = {
        "status": "PARSED",
        "parser": "DocxParser",
        "source_file_type": "DOCX",
        "text": "\n".join(_TEXTS),
        "paragraphs": paragraphs,
    }
    payload.update(overrides)
    return ParseResult(**payload)


def _clause(**overrides: Any) -> Clause:
    payload: dict[str, Any] = {
        "clause_index": 0,
        "clause_no": "第一条",
        "title": "知识产权",
        "clause_type": "IP",
        "start_paragraph_index": 0,
        "end_paragraph_index": 1,
        "text": "\n".join(_TEXTS[:2]),
        "extract_method": "RULE",
    }
    payload.update(overrides)
    return Clause(**payload)


def _metadata(**overrides: Any) -> MetadataItem:
    payload: dict[str, Any] = {
        "field_key": "counterparty_name",
        "field_label": "相对方名称",
        "field_value": "乙方",
        "value_type": "TEXT",
        "paragraph_index": 2,
        "quote": "第二条 违约责任",
        "extract_method": "REGEX",
    }
    payload.update(overrides)
    return MetadataItem(**payload)


@pytest.fixture
async def make_backend() -> Any:
    opened: list[httpx.AsyncClient] = []

    def _make(handler: Handler) -> BackendClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opened.append(client)
        return BackendClient(BACKEND_BASE_URL, client=client)

    yield _make

    for client in opened:
        await client.aclose()


def _ok_handler(captured: list[dict[str, Any]] | None = None) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(json.loads((await request.aread()).decode()))
        return httpx.Response(201, json=_OK_BODY)

    return handler


async def _run(make_backend: Any, **overrides: Any) -> Any:
    captured: list[dict[str, Any]] = []
    request = DocumentPersistenceRequest(
        task_id=33,
        parse_result=overrides.pop("parse_result", _parse_result()),
        clauses=overrides.pop("clauses", [_clause()]),
        metadata=overrides.pop("metadata", [_metadata()]),
    )
    result = await DocumentPersistenceTool(make_backend(_ok_handler(captured))).run(request)
    return result, captured[0]


# --------------------------------------------------------------------------- #
# 块映射
# --------------------------------------------------------------------------- #
async def test_a_normal_paragraph_maps_field_by_field(make_backend: Any) -> None:
    _, body = await _run(make_backend)

    assert body["blocks"][0] == {
        "order_index": 0,
        "paragraph_index": 0,
        "block_type": "PARAGRAPH",
        "text": _TEXTS[0],
        "raw_text": _TEXTS[0],
        "char_start_in_block": 0,
        "char_end_in_block": len(_TEXTS[0]),
        "char_start_global": 0,
        "char_end_global": len(_TEXTS[0]),
    }


async def test_a_table_row_keeps_its_block_type(make_backend: Any) -> None:
    """``TABLE_ROW`` 是**结构信息**，不是从文本猜的 —— 映射必须原样搬运。

    丢成 ``PARAGRAPH`` 会让前端把表格行当正文段落渲染。
    """
    _, body = await _run(make_backend)

    assert [block["block_type"] for block in body["blocks"]] == [
        "PARAGRAPH",
        "TABLE_ROW",
        "PARAGRAPH",
    ]


async def test_the_paragraph_index_equals_the_block_position(make_backend: Any) -> None:
    """本项目里两者恰好相等（段落号从 0 连续递增、blocks 同序），把这个前提钉住。

    条款与元数据的换算依赖它 —— 一旦不等，``_block_positions`` 仍然正确，
    但直接按下标赋值的写法就会开始出错。
    """
    _, body = await _run(make_backend)

    for position, block in enumerate(body["blocks"]):
        assert block["paragraph_index"] == position
        assert block["order_index"] == position


# --------------------------------------------------------------------------- #
# 全局字符偏移
# --------------------------------------------------------------------------- #
async def test_the_global_offsets_can_cut_the_paragraph_back_out(make_backend: Any) -> None:
    """**坐标的可验证不变量**：用算出来的区间去切全文，必须恰好得到那一段。

    这比"偏移等于某个数字"强得多 —— 它同时验证了累加口径（含 ``\\n`` 占位）
    与段落顺序。
    """
    parse_result = _parse_result()
    _, body = await _run(make_backend, parse_result=parse_result)

    for block in body["blocks"]:
        assert parse_result.text[block["char_start_global"] : block["char_end_global"]] == block["text"]


async def test_the_offsets_are_contiguous_with_newline_gaps(make_backend: Any) -> None:
    """段落之间恰好隔一个 ``\\n`` —— 这正是 ``ParseResult.text`` 的拼接口径。"""
    parse_result = _parse_result()
    _, body = await _run(make_backend, parse_result=parse_result)

    blocks = body["blocks"]
    for previous, current in itertools.pairwise(blocks):
        assert current["char_start_global"] == previous["char_end_global"] + 1
        assert parse_result.text[previous["char_end_global"]] == "\n"


async def test_an_empty_text_paragraph_keeps_a_zero_width_range(make_backend: Any) -> None:
    """空段落（清洗后是空串）不丢弃 —— 它的区间是零宽的，但仍占一个段落号。

    丢掉它会让**后面所有段落的编号整体前移**，条款与风险的坐标全部错位。
    """
    parse_result = _parse_result(
        paragraphs=[
            Paragraph(index=0, block_type="PARAGRAPH", text="甲"),
            Paragraph(index=1, block_type="PARAGRAPH", text=""),
            Paragraph(index=2, block_type="PARAGRAPH", text="乙"),
        ],
        text="甲\n\n乙",
    )
    _, body = await _run(make_backend, parse_result=parse_result, clauses=[], metadata=[])

    assert len(body["blocks"]) == 3
    assert body["blocks"][1]["char_start_in_block"] == 0
    assert body["blocks"][1]["char_end_in_block"] == 0
    for block in body["blocks"]:
        assert parse_result.text[block["char_start_global"] : block["char_end_global"]] == block["text"]


# --------------------------------------------------------------------------- #
# 条款映射
# --------------------------------------------------------------------------- #
async def test_a_clause_range_is_translated_to_block_positions(make_backend: Any) -> None:
    """段落号 → ``blocks[]`` 下标。两者今天相等，但换算**不靠这个巧合**。"""
    _, body = await _run(make_backend, clauses=[_clause(start_paragraph_index=1, end_paragraph_index=2)])

    (clause,) = body["clauses"]
    assert (clause["start_block_index"], clause["end_block_index"]) == (1, 2)


async def test_a_clause_carries_its_number_and_title(make_backend: Any) -> None:
    """``clause_no`` / ``title`` 原样带上 —— Backend 的 ``clause`` 表有这两列，
    前端要显示「第三条 · 知识产权」。丢弃它们等于故意扔掉接收方明确要的数据。"""
    _, body = await _run(make_backend)

    (clause,) = body["clauses"]
    assert clause["clause_no"] == "第一条"
    assert clause["title"] == "知识产权"
    assert clause["clause_type"] == "IP"
    assert clause["extract_method"] == "RULE"


async def test_a_single_paragraph_clause_maps_to_a_one_block_range(make_backend: Any) -> None:
    _, body = await _run(make_backend, clauses=[_clause(start_paragraph_index=2, end_paragraph_index=2)])

    (clause,) = body["clauses"]
    assert clause["start_block_index"] == clause["end_block_index"] == 2


@pytest.mark.parametrize(
    ("start", "end", "hint"),
    [
        (5, 6, "不存在的段落号"),
        (0, 9, "不存在的段落号"),
    ],
)
async def test_a_clause_referencing_a_missing_paragraph_is_rejected(
    start: int, end: int, hint: str, make_backend: Any
) -> None:
    """越界引用**在 Agent 侧就被拒绝**，不会生成一个越界的 DTO 发给 Backend。

    发给 Backend 只会得到一句含糊的"请求校验失败"，而真正的问题
    ——"条款切分引用了一个根本不存在的段落"—— 就看不见了。
    """
    with pytest.raises(DocumentMappingError) as excinfo:
        await _run(make_backend, clauses=[_clause(start_paragraph_index=start, end_paragraph_index=end)])

    assert hint in str(excinfo.value)
    assert "自相矛盾" in str(excinfo.value)


async def test_a_backwards_clause_range_is_rejected(make_backend: Any) -> None:
    with pytest.raises(DocumentMappingError) as excinfo:
        await _run(make_backend, clauses=[_clause(start_paragraph_index=2, end_paragraph_index=0)])

    assert "区间倒置" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 元数据映射
# --------------------------------------------------------------------------- #
async def test_metadata_source_is_translated_to_a_block_position(make_backend: Any) -> None:
    _, body = await _run(make_backend, metadata=[_metadata(paragraph_index=2)])

    assert body["metadata"][0]["source_block_index"] == 2


async def test_metadata_maps_its_business_fields(make_backend: Any) -> None:
    _, body = await _run(make_backend)

    assert body["metadata"][0] == {
        "field_key": "counterparty_name",
        "field_label": "相对方名称",
        "field_value": "乙方",
        "value_type": "TEXT",
        "extract_method": "REGEX",
        "source_block_index": 2,
    }


async def test_the_metadata_quote_is_not_sent(make_backend: Any) -> None:
    """``contract_metadata`` 没有 quote 列，定位由 ``source_block_id`` 表达。

    这是**已知的信息丢弃**（P10-1 的映射清单即如此），把它钉住是为了让
    "哪天想保留它就发现没发出去"这件事是显式的，而不是靠读代码才发现。
    """
    _, body = await _run(make_backend, metadata=[_metadata(quote="不该出现的引文")])

    assert "quote" not in body["metadata"][0]
    assert "不该出现的引文" not in json.dumps(body, ensure_ascii=False)


async def test_metadata_referencing_a_missing_paragraph_is_rejected(make_backend: Any) -> None:
    with pytest.raises(DocumentMappingError) as excinfo:
        await _run(make_backend, metadata=[_metadata(paragraph_index=99)])

    assert "counterparty_name" in str(excinfo.value)
    assert "不存在的段落号 99" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# parse_status
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["PARSED", "EMPTY"])
async def test_a_successful_parse_maps_to_parsed(status: str, make_backend: Any) -> None:
    """``EMPTY`` 与 ``PARSED`` 同路：解析**成功**了，只是文档没有正文。

    它是数据问题，用"零个块"表达即可 —— ``ParseStatus`` 里没有 EMPTY，
    也不该为了它新造一个。
    """
    parse_result = _parse_result(status=status)

    _, body = await _run(make_backend, parse_result=parse_result)

    assert body["parse_status"] == "PARSED"


async def test_a_failed_parse_maps_to_failed(make_backend: Any) -> None:
    parse_result = _parse_result(status="FAILED", text="", paragraphs=[])

    _, body = await _run(make_backend, parse_result=parse_result, clauses=[], metadata=[])

    assert body["parse_status"] == "FAILED"
    assert body["blocks"] == []


async def test_a_failed_parse_is_still_sent(make_backend: Any) -> None:
    """``FAILED`` 也要发请求 —— 跳过它会让 ``parse_status`` 永远停在 ``PENDING``，
    而且再也无法被后续任务升级（P10-1 的迁移只从 ``PENDING`` / ``FAILED`` 出发）。"""
    urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(201, json={**_OK_BODY, "parse_status": "FAILED", "blocks_created": 0})

    request = DocumentPersistenceRequest(
        task_id=33, parse_result=_parse_result(status="FAILED", text="", paragraphs=[])
    )
    result = await DocumentPersistenceTool(make_backend(handler)).run(request)

    assert urls == [EXPECTED_URL]
    assert result.ok is True
    assert result.parse_status == "FAILED"


# --------------------------------------------------------------------------- #
# 调用与响应
# --------------------------------------------------------------------------- #
async def test_it_posts_to_the_task_scoped_endpoint(make_backend: Any) -> None:
    methods: list[str] = []
    urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        urls.append(str(request.url))
        return httpx.Response(201, json=_OK_BODY)

    request = DocumentPersistenceRequest(task_id=33, parse_result=_parse_result())
    await DocumentPersistenceTool(make_backend(handler)).run(request)

    assert urls == [EXPECTED_URL], "任务是路径的一部分，请求体里不再重复一遍 task_id"
    assert methods == ["POST"]


async def test_the_response_is_read_defensively(make_backend: Any) -> None:
    result, _ = await _run(make_backend)

    assert (
        result.blocks_created,
        result.blocks_reused,
        result.clauses_persisted,
        result.metadata_persisted,
        result.current_stage,
    ) == (3, 0, 1, 1, "CLAUSED")


@pytest.mark.parametrize(
    "code",
    ["DOCUMENT_ALREADY_PERSISTED", "DOCUMENT_BLOCKS_CONFLICT", "PARSE_STATUS_ALREADY_FINAL"],
)
async def test_a_backend_rejection_is_reported_verbatim(code: str, make_backend: Any) -> None:
    """Backend 的错误码原样带回 —— 翻译会丢掉"到底是谁拒绝的、为什么"。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": code, "message": "拒绝"})

    request = DocumentPersistenceRequest(task_id=33, parse_result=_parse_result())
    result = await DocumentPersistenceTool(make_backend(handler)).run(request)

    assert result.ok is False
    assert result.error_code == code
    assert result.error_message == "拒绝"
    assert result.current_stage is None


async def test_an_unreachable_backend_is_reported(make_backend: Any) -> None:
    from app.core.errors import AgentErrorCode

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    request = DocumentPersistenceRequest(task_id=33, parse_result=_parse_result())
    result = await DocumentPersistenceTool(make_backend(handler)).run(request)

    assert result.ok is False
    assert result.error_code == AgentErrorCode.BACKEND_UNREACHABLE.value


async def test_a_non_json_success_body_is_reported(make_backend: Any) -> None:
    from app.core.errors import AgentErrorCode

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"<html>ok</html>")

    request = DocumentPersistenceRequest(task_id=33, parse_result=_parse_result())
    result = await DocumentPersistenceTool(make_backend(handler)).run(request)

    assert result.ok is False
    assert result.error_code == AgentErrorCode.BACKEND_REJECTED.value


async def test_the_three_artifacts_go_in_one_request(make_backend: Any) -> None:
    """块/条款/元数据**必须一起发** —— 它们在 Backend 侧是一个事务，
    clause/metadata 要引用本次写入的 block.id。拆成三次调用就拼不起来了。"""
    _, body = await _run(make_backend)

    assert set(body) == {"parse_status", "blocks", "clauses", "metadata"}
    assert body["blocks"] and body["clauses"] and body["metadata"]
