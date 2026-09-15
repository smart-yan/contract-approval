"""P5-3 最小 Graph 的端到端行为。

只 mock **网络层**（``httpx.MockTransport``），不 mock Agent 自己的节点 ——
这样 multipart 编码、响应解析、State 流转、Conditional Edge 分流全都真实执行，
测出来的才是"Graph 真的跑起来了"，而不是"我 mock 的东西按我想的返回了"。

四个用例对应四类走向：

============================  ==========================================
用例                           期望
============================  ==========================================
合法文件                        upload → validate → parse → END
非法文件（Backend 415）         upload → validate → END（不进入 parse）
Backend 不可达                  upload → validate → END
成功响应但字段不全               upload → validate → END
============================  ==========================================
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.errors import AgentErrorCode
from app.graph.builder import build_review_graph
from app.graph.context import ReviewContext
from app.rules.schemas import AgentRule, RuleSetSnapshot
from app.tools.backend_client import BackendClient
from tests.factories import docx_bytes

BACKEND_BASE_URL = "http://backend.test"
EXPECTED_UPLOAD_URL = f"{BACKEND_BASE_URL}/api/v1/contracts"

#: Backend ``POST /api/v1/contracts`` 成功响应的真实形状（见 backend/app/schemas/contract.py）
SUCCESS_PAYLOAD: dict[str, Any] = {
    "contract_id": 11,
    "contract_no": "HT-2026-001",
    "title": "设备采购合同",
    "contract_type": "PURCHASE",
    "contract_status": "PENDING",
    "file_id": 22,
    "filename": "contract.docx",
    "file_size": 1234,
    "file_type": "DOCX",
    "sha256": "a" * 64,
    "parse_status": "PENDING",
    "review_task_id": 33,
    "task_status": "PENDING",
    "task_stage": "UPLOADED",
    "reused": False,
}

#: 一份**真实**的 DOCX（P6-1 起 parse_document 会真的去解析它）
DOCX_PARAGRAPHS = ("甲方：某某科技有限公司", "第一条 本合同自双方签字之日起生效。")
DOCX_BYTES = docx_bytes(*DOCX_PARAGRAPHS)

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]

#: 一份**合法**的 PURCHASE 规则集快照（与 seed 的规则同形，取 1 条即可）——
#: 规则审查的合法前置输入，见 ``_initial_state`` 的说明。
PURCHASE_SNAPSHOT = RuleSetSnapshot(
    contract_type="PURCHASE",
    rule_set_version="v1",
    rules=[
        AgentRule(
            rule_code="IP_OWNER_SUPPLIER_001",
            rule_name="知识产权归属相对方",
            dimension="知识产权",
            rule_type="KEYWORD",
            expression={"keywords": ["知识产权归乙方"], "logic": "ANY"},
            target_clause_types=["IP"],
            severity="HIGH",
            sort_order=10,
        )
    ],
)


@pytest.fixture
async def make_backend() -> AsyncIterator[Callable[[Handler], BackendClient]]:
    """构造一个挂了 MockTransport 的 BackendClient，并在用例结束时关闭底层 client。

    Agent 侧的 ``BackendClient`` 接受注入的 ``httpx.AsyncClient``，
    所以这里不需要 monkeypatch，也不需要起真实 Backend —— 这正是把依赖放进
    ``ReviewContext`` 而不是全局单例的收益。
    """
    opened: list[httpx.AsyncClient] = []

    def _make(handler: Handler) -> BackendClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opened.append(client)
        return BackendClient(BACKEND_BASE_URL, client=client)

    yield _make

    for client in opened:
        await client.aclose()


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    path = tmp_path / "contract.docx"
    path.write_bytes(DOCX_BYTES)
    return path


def _initial_state(source_file: Path) -> dict[str, Any]:
    return {
        "file_path": str(source_file),
        "filename": "contract.docx",
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "contract_no": "HT-2026-001",
        "title": "设备采购合同",
        "contract_type": "PURCHASE",
        # 规则快照是规则审查的**合法前置输入**（P8-2）：没有它，
        # ``rule_review`` 会按"输入缺失"判定失败，成功路径就不再成功。
        # 走不到 rule_review 的用例带上它也无副作用。
        "rule_snapshot": PURCHASE_SNAPSHOT,
    }


async def _run(backend: BackendClient, source_file: Path) -> dict[str, Any]:
    graph = build_review_graph()
    return await graph.ainvoke(
        _initial_state(source_file),
        context=ReviewContext(backend=backend),
    )


# --------------------------------------------------------------------------- #
# Case 1：合法文件 —— upload_file → validate_file → parse_document → END
# --------------------------------------------------------------------------- #
async def test_valid_file_runs_through_to_parse(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    requests: list[httpx.Request] = []
    bodies: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        bodies.append(await request.aread())
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    final = await _run(make_backend(handler), source_file)

    # 1) 确实调用了 Backend 的 P4 接入接口，而不是 Agent 自己解析/落盘
    #    ⚠️ P9-10 起图尾还会调一次风险写入接口（``/review-tasks/{id}/risks``），
    #    因此这里断言的是**第一次**调用，而不是"总共只有一次"
    assert requests[0].method == "POST"
    assert str(requests[0].url) == EXPECTED_UPLOAD_URL
    assert requests[0].headers["content-type"].startswith("multipart/form-data;")
    # 表单里带上了 Backend 要求的业务字段，文件字节原样送达
    assert b'name="contract_no"' in bodies[0]
    assert b"HT-2026-001" in bodies[0]
    assert b'name="contract_type"' in bodies[0]
    assert DOCX_BYTES in bodies[0]

    # 2) Backend 返回的标识被带进了 State
    assert final["contract_id"] == 11
    assert final["file_id"] == 22
    assert final["review_task_id"] == 33
    assert final["sha256"] == "a" * 64
    assert final["reused"] is False

    # 3) 门禁通过，且 Conditional Edge 确实走了 continue 分支
    assert final["file_valid"] is True
    assert final["validation_errors"] == []
    assert final.get("error_code") is None

    # 4) 解析节点真的把 DOCX 解析成了结构化文档（P6-1 起不再是桩）
    parse_result = final["parse_result"]
    assert parse_result.status == "PARSED"
    assert parse_result.parser == "DocxParser"
    assert parse_result.source_file_type == "DOCX"
    assert [p.text for p in parse_result.paragraphs] == list(DOCX_PARAGRAPHS)
    assert parse_result.text == "\n".join(DOCX_PARAGRAPHS)


# --------------------------------------------------------------------------- #
# Case 1b：可上传但解析不出来 —— 图必须经由 parse 后的条件边收尾
# --------------------------------------------------------------------------- #
async def test_unparsable_document_ends_the_workflow_after_parse(
    make_backend: Callable[[Handler], BackendClient], tmp_path: Path
) -> None:
    """内容不是合法 DOCX：解析失败，工作流在 parse 之后的条件边处结束。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip archive")

    final = await _run(make_backend(handler), broken)

    assert final["file_valid"] is True, "门禁过了 —— 问题出在解析，不是上传"
    assert final["parse_result"].status == "FAILED"
    assert final["parse_result"].parser == "DocxParser"
    assert final["error_code"] == AgentErrorCode.PARSE_FAILED.value
    assert final["parse_result"].paragraphs == []


# --------------------------------------------------------------------------- #
# Case 2：非法文件 —— upload_file → validate_file → END（parse_document 不执行）
# --------------------------------------------------------------------------- #
async def test_invalid_file_stops_before_parse(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """Backend 以 415 拒绝（真实 Backend 对不支持的格式正是这么回的）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            415,
            json={
                "code": "UNSUPPORTED_FORMAT",
                "message": "不支持的文件扩展名：.exe",
                "details": {"allowed_extensions": [".docx", ".pdf", ".jpg", ".jpeg", ".png"]},
                "request_id": "req-test",
            },
        )

    final = await _run(make_backend(handler), source_file)

    # 保留 Backend 的稳定错误码，而不是换成 Agent 自己的说法
    assert final["file_valid"] is False
    assert final["error_code"] == "UNSUPPORTED_FORMAT"
    assert final["error_message"] == "不支持的文件扩展名：.exe"
    assert final["validation_errors"]

    # 关键断言：Conditional Edge 走了 stop 分支，解析节点**根本没有执行**
    assert "parse_result" not in final


# --------------------------------------------------------------------------- #
# 附加：Backend 不可达 —— 同样停在 END，不进入解析
# --------------------------------------------------------------------------- #
async def test_backend_unreachable_stops_before_parse(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    final = await _run(make_backend(handler), source_file)

    assert final["file_valid"] is False
    assert final["error_code"] == "BACKEND_UNREACHABLE"
    assert "parse_result" not in final


# --------------------------------------------------------------------------- #
# 附加：Backend 返回 2xx 但字段不全 —— 门禁仍然拦得住
# --------------------------------------------------------------------------- #
async def test_incomplete_success_payload_stops_before_parse(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """契约完整性检查：Backend 改了成功响应字段时，问题在门禁处立刻暴露。"""
    incomplete = {k: v for k, v in SUCCESS_PAYLOAD.items() if k != "review_task_id"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=incomplete)

    final = await _run(make_backend(handler), source_file)

    assert final["file_valid"] is False
    assert final["error_code"] == "BACKEND_CONTRACT_INCOMPLETE"
    assert any("review_task_id" in e for e in final["validation_errors"])
    assert "parse_result" not in final


# --------------------------------------------------------------------------- #
# 附加：输入残缺 —— 连 Backend 都不该调用
# --------------------------------------------------------------------------- #
async def test_missing_input_never_calls_backend(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    backend = make_backend(handler)
    graph = build_review_graph()
    state = _initial_state(source_file)
    del state["contract_no"]  # 调用方漏传必需字段

    final = await graph.ainvoke(state, context=ReviewContext(backend=backend))

    assert calls == []
    assert final["file_valid"] is False
    assert final["error_code"] == "AGENT_INPUT_INVALID"
    assert "parse_result" not in final
