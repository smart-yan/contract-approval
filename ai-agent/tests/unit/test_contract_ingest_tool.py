"""``ContractIngestTool`` 的单元测试。

只 mock **网络层**（``httpx.MockTransport``）：Tool 的输入/输出契约、
字段映射、错误翻译、防御性取值全部真实执行。

这一层不装配 Graph —— 那是 ``tests/integration/test_minimal_review_graph.py``
和 ``test_review_endpoint.py`` 的事。这里只回答一个问题：
**"给定 Backend 的各种返回，Tool 会吐出什么？"**
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.tools.backend_client import BackendClient
from app.tools.contract_ingest import ContractIngestRequest, ContractIngestTool

BACKEND_BASE_URL = "http://backend.test"
EXPECTED_UPLOAD_URL = f"{BACKEND_BASE_URL}/api/v1/contracts"

#: Backend ``POST /api/v1/contracts`` 成功响应的真实形状
SUCCESS_PAYLOAD: dict[str, Any] = {
    "contract_id": 11,
    "contract_no": "HT-2026-001",
    "title": "设备采购合同",
    "contract_type": "PURCHASE",
    "contract_status": "pending",
    "file_id": 22,
    "filename": "contract.docx",
    "file_size": 1234,
    "file_type": "DOCX",
    "sha256": "a" * 64,
    "parse_status": "PENDING",
    "review_task_id": 33,
    "task_status": "pending",
    "task_stage": "UPLOADED",
    "reused": False,
    "task_reused": False,
}

DOCX_BYTES = b"PK\x03\x04 minimal docx fixture"

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]


@pytest.fixture
async def make_tool() -> AsyncIterator[Callable[[Handler], ContractIngestTool]]:
    """构造一个挂了 MockTransport 的 Tool，并在用例结束时关闭底层 client。"""
    opened: list[httpx.AsyncClient] = []

    def _make(handler: Handler) -> ContractIngestTool:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opened.append(client)
        return ContractIngestTool(BackendClient(BACKEND_BASE_URL, client=client))

    yield _make

    for client in opened:
        await client.aclose()


@pytest.fixture
def request_(tmp_path: Path) -> ContractIngestRequest:
    path = tmp_path / "contract.docx"
    path.write_bytes(DOCX_BYTES)
    return ContractIngestRequest(
        file_path=path,
        filename="contract.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        contract_no="HT-2026-001",
        title="设备采购合同",
        contract_type="PURCHASE",
    )


def _responding(payload: dict[str, Any], status_code: int = 201) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


# --------------------------------------------------------------------------- #
# 正常上传：输入被完整送出，输出字段被完整带回
# --------------------------------------------------------------------------- #
async def test_success_maps_every_business_field(
    make_tool: Callable[[Handler], ContractIngestTool], request_: ContractIngestRequest
) -> None:
    result = await make_tool(_responding(SUCCESS_PAYLOAD)).run(request_)

    assert result.ok is True
    assert result.contract_id == 11
    assert result.file_id == 22
    assert result.review_task_id == 33
    assert result.sha256 == "a" * 64
    assert result.file_type == "DOCX"
    assert result.file_size == 1234
    assert result.reused is False
    assert result.task_reused is False
    assert result.error_code is None
    assert result.error_message is None


async def test_request_is_forwarded_verbatim(
    make_tool: Callable[[Handler], ContractIngestTool], request_: ContractIngestRequest
) -> None:
    """Tool 不做任何加工：文件字节与业务字段原样送给 Backend。"""
    seen: list[tuple[str, bytes]] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append((str(http_request.url), await http_request.aread()))
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    await make_tool(handler).run(request_)

    assert len(seen) == 1
    url, body = seen[0]
    assert url == EXPECTED_UPLOAD_URL
    assert DOCX_BYTES in body, "文件字节必须原样转发"
    assert b'name="contract_no"' in body
    assert b"HT-2026-001" in body
    assert b'name="contract_type"' in body
    assert b"PURCHASE" in body


# --------------------------------------------------------------------------- #
# 两层幂等的信号必须透传
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("reused", "task_reused"), [(False, False), (True, True), (True, False)])
async def test_idempotency_signals_are_passed_through(
    make_tool: Callable[[Handler], ContractIngestTool],
    request_: ContractIngestRequest,
    reused: bool,
    task_reused: bool,
) -> None:
    payload = {**SUCCESS_PAYLOAD, "reused": reused, "task_reused": task_reused}

    result = await make_tool(_responding(payload)).run(request_)

    assert result.ok is True
    assert result.reused is reused
    assert result.task_reused is task_reused


# --------------------------------------------------------------------------- #
# 失败：Backend 拒绝 / 不可达
# --------------------------------------------------------------------------- #
async def test_backend_rejection_keeps_backend_error_code(
    make_tool: Callable[[Handler], ContractIngestTool], request_: ContractIngestRequest
) -> None:
    """Backend 的稳定错误码必须原样保留 —— 不要换成 Agent 自己的说法。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            415,
            json={"code": "UNSUPPORTED_FORMAT", "message": "不支持的文件扩展名", "details": {}},
        )

    result = await make_tool(handler).run(request_)

    assert result.ok is False
    assert result.error_code == "UNSUPPORTED_FORMAT"
    assert result.error_message == "不支持的文件扩展名"
    # 失败时不得泄漏任何"看起来成功"的字段
    assert result.contract_id is None
    assert result.file_id is None
    assert result.review_task_id is None
    assert result.reused is None
    assert result.task_reused is None


async def test_backend_unreachable_is_reported_not_raised(
    make_tool: Callable[[Handler], ContractIngestTool], request_: ContractIngestRequest
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await make_tool(handler).run(request_)

    assert result.ok is False
    assert result.error_code == "BACKEND_UNREACHABLE"
    assert result.contract_id is None


async def test_missing_local_file_is_reported_not_raised(
    make_tool: Callable[[Handler], ContractIngestTool], tmp_path: Path
) -> None:
    """文件在本机读不到时也走"结果"而不是异常 —— 否则会炸掉整张图。"""

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("读不到文件就不应该发请求")

    missing = ContractIngestRequest(
        file_path=tmp_path / "not-there.docx",
        filename="not-there.docx",
        contract_no="HT-1",
        title="T",
        contract_type="PURCHASE",
    )
    result = await make_tool(handler).run(missing)

    assert result.ok is False
    assert result.error_code == "AGENT_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# 防御性取值：Backend 返回了意外内容时，宁可给出 None，也不要在几个节点之后炸
# --------------------------------------------------------------------------- #
async def test_missing_fields_become_none_instead_of_erroring(
    make_tool: Callable[[Handler], ContractIngestTool], request_: ContractIngestRequest
) -> None:
    """缺字段时不抛异常 —— 由 validate_file 这个 Workflow Gate 去判定并报告。"""

    result = await make_tool(_responding({"contract_id": 11})).run(request_)

    assert result.ok is True
    assert result.contract_id == 11
    assert result.file_id is None
    assert result.review_task_id is None
    assert result.sha256 is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("11", None),  # 字符串 ID
        (True, None),  # bool 是 int 的子类，不能当成合法 ID
        (1.5, None),  # 浮点
        (None, None),
        (11, 11),
    ],
)
async def test_non_integer_id_is_rejected(
    make_tool: Callable[[Handler], ContractIngestTool],
    request_: ContractIngestRequest,
    raw: Any,
    expected: int | None,
) -> None:
    payload = {**SUCCESS_PAYLOAD, "contract_id": raw}

    result = await make_tool(_responding(payload)).run(request_)

    assert result.contract_id == expected


async def test_success_response_that_is_not_json_is_reported(
    make_tool: Callable[[Handler], ContractIngestTool], request_: ContractIngestRequest
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, text="<html>gateway</html>")

    result = await make_tool(handler).run(request_)

    assert result.ok is False
    assert result.error_code == "BACKEND_REJECTED"


# --------------------------------------------------------------------------- #
# business_fields()：State 合并用的字段视图
# --------------------------------------------------------------------------- #
async def test_business_fields_drops_none_but_keeps_false(
    make_tool: Callable[[Handler], ContractIngestTool], request_: ContractIngestRequest
) -> None:
    """``False`` 是有意义的取值（"没复用"），必须保留；``None`` 才是"没有这个字段"。"""
    result = await make_tool(_responding(SUCCESS_PAYLOAD)).run(request_)

    fields_ = result.business_fields()

    assert fields_ == {
        "contract_id": 11,
        "file_id": 22,
        "review_task_id": 33,
        "sha256": "a" * 64,
        "file_type": "DOCX",
        "file_size": 1234,
        "reused": False,
        "task_reused": False,
    }


async def test_business_fields_excludes_failure_keys(
    make_tool: Callable[[Handler], ContractIngestTool], request_: ContractIngestRequest
) -> None:
    """``ok`` / ``error_code`` / ``error_message`` 不是业务字段，不能混进 State 更新。"""
    result = await make_tool(_responding(SUCCESS_PAYLOAD)).run(request_)

    fields_ = result.business_fields()

    assert "ok" not in fields_
    assert "error_code" not in fields_
    assert "error_message" not in fields_
