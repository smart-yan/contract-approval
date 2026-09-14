"""``POST /api/agent/review`` 的端到端行为。

只 mock **网络层**（``httpx.MockTransport``），不 mock Graph、不 mock 节点 ——
multipart 编码、临时文件落盘、State 流转、Conditional Edge 分流、
状态码与响应投影全部真实执行。

Agent 在这一跳上只做传输：文件与元数据原样转发给 Backend，
所有业务判断（类型/大小/幂等）都由 Backend 完成。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable, Coroutine, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app.api.review as review_module
from app.main import app
from app.rules.schemas import AgentRule, RuleSetSnapshot
from app.tools.backend_client import BackendClient
from tests.factories import docx_bytes

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

#: 一份**真实**的 DOCX（P6-1 起 parse_document 会真的去解析它）
DOCX_PARAGRAPHS = ("甲方：某某科技有限公司", "第一条 本合同自双方签字之日起生效。")
DOCX_BYTES = docx_bytes(*DOCX_PARAGRAPHS)

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]

#: 一份**合法**的 PURCHASE 规则集快照（与 seed 的规则同形）。
#: 只放 1 条就够 —— 这两个 happy path 用例断言的是"上传 → 解析"这条链路，
#: 规则审查本身由 ``test_rule_review_graph.py`` 覆盖。
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


@dataclass
class _SnapshotInjectingGraph:
    """在 Graph 的**初始 State** 里补上 ``rule_snapshot`` —— 模拟编排层的注入。

    ⚠️ **生产代码目前没有这一步**：``run_review`` 组装的初始 State 只有上传字段，
    "规则集怎么从 Backend 取、由谁放进 State" 是 P8-2 的下一小步。
    这里补上它，是为了让 happy path 具备规则审查所需的**合法前置输入**。

    这不是"绕开 rule_review"：节点照常执行、照常逐条求值 ——
    只是把当前缺失的那一环在测试里显式地补出来。
    """

    graph: Any
    snapshot: RuleSetSnapshot

    async def ainvoke(self, state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await self.graph.ainvoke({**state, "rule_snapshot": self.snapshot}, **kwargs)


@pytest.fixture
def agent() -> Iterator[Callable[..., TestClient]]:
    """启动 Agent 应用，并把它的 ``BackendClient`` 换成挂了 MockTransport 的实例。

    ``app.state.backend_client`` 由 lifespan 创建；这里在启动后替换它，
    因此不需要 monkeypatch 全局、也不需要起真实 Backend ——
    这正是把依赖放进 ``app.state`` 而不是模块级单例的收益。

    :param snapshot: 传入时会包一层 :class:`_SnapshotInjectingGraph` —— 见该类的说明。
    """
    with ExitStack() as stack:
        opened: list[httpx.AsyncClient] = []

        def _make(handler: Handler, snapshot: RuleSetSnapshot | None = None) -> TestClient:
            test_client = stack.enter_context(TestClient(app))
            transport = httpx.MockTransport(handler)
            http_client = httpx.AsyncClient(transport=transport)
            opened.append(http_client)
            app.state.backend_client = BackendClient(BACKEND_BASE_URL, client=http_client)
            if snapshot is not None:
                # lifespan 已经建好了图，这里再包一层（必须在 enter_context 之后）
                app.state.review_graph = _SnapshotInjectingGraph(app.state.review_graph, snapshot)
            return test_client

        yield _make

        for http_client in opened:
            # MockTransport 不持有连接，但显式关闭更干净
            asyncio.run(http_client.aclose())


def _post(test_client: TestClient, payload: bytes = DOCX_BYTES) -> Any:
    return test_client.post(
        "/api/agent/review",
        files={
            "file": (
                "contract.docx",
                payload,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={
            "contract_no": "HT-2026-001",
            "title": "设备采购合同",
            "contract_type": "PURCHASE",
        },
    )


# --------------------------------------------------------------------------- #
# 健康检查（lifespan 改动后的最小回归）
# --------------------------------------------------------------------------- #
def test_health_still_reports_service_state(agent: Callable[[Handler], TestClient]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不会被调用
        raise AssertionError("健康检查不应触发任何出站请求")

    test_client = agent(handler)
    response = test_client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "ai-agent"
    assert body["status"] == "ok"


# --------------------------------------------------------------------------- #
# 合法文件 —— 走完 upload → validate → parse
# --------------------------------------------------------------------------- #
def test_valid_file_runs_the_whole_workflow(agent: Callable[[Handler], TestClient]) -> None:
    requests: list[httpx.Request] = []
    bodies: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        bodies.append(await request.aread())
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler, snapshot=PURCHASE_SNAPSHOT))

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_status"] == "completed"

    # 1) 确实把文件转发给了 Backend 的接入接口
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == EXPECTED_UPLOAD_URL
    assert requests[0].headers["content-type"].startswith("multipart/form-data;")
    assert b'name="contract_no"' in bodies[0]
    assert DOCX_BYTES in bodies[0], "文件字节必须原样转发"

    # 2) Backend 返回的标识与幂等信号被透传
    assert body["contract_id"] == 11
    assert body["file_id"] == 22
    assert body["review_task_id"] == 33
    assert body["sha256"] == "a" * 64
    assert body["reused"] is False
    assert body["task_reused"] is False

    # 3) 解析节点真的解析了 DOCX（P6-1 起不再是桩）
    assert body["parse_result"]["status"] == "PARSED"
    assert body["parse_result"]["parser"] == "DocxParser"
    assert body["parse_result"]["source_file_type"] == "DOCX"
    assert [p["text"] for p in body["parse_result"]["paragraphs"]] == list(DOCX_PARAGRAPHS)
    assert body["parse_result"]["text"] == "\n".join(DOCX_PARAGRAPHS)
    assert body["validation_errors"] == []
    assert body["error_code"] is None


# --------------------------------------------------------------------------- #
# 解析失败 / 空文档 —— workflow_status 必须与 parse_result 一致
# --------------------------------------------------------------------------- #
def test_unparsable_document_is_never_reported_as_completed(
    agent: Callable[[Handler], TestClient],
) -> None:
    """核心不变量：``parse_result.status == "FAILED"`` ⇒ ``workflow_status != "completed"``。

    这份文件上传能过（Backend 认它是 DOCX），但内容不是合法的 ZIP ——
    解析读不出来。工作流**没有**产出任何可用内容，就不能说它完成了。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler), payload=b"this is not a zip archive at all")

    assert response.status_code == 422, "没产出可用文档就不该是 2xx"
    body = response.json()

    assert body["parse_result"]["status"] == "FAILED"
    assert body["workflow_status"] != "completed"
    assert body["workflow_status"] == "rejected"
    assert body["error_code"] == "PARSE_FAILED"
    assert body["parse_result"]["error_code"] == "PARSE_FAILED"
    assert body["parse_result"]["paragraphs"] == []
    # 上传本身是成功的 —— 失败发生在解析，不要把它误报成上传问题
    assert body["contract_id"] == 11
    assert body["validation_errors"] == []


def test_missing_rule_snapshot_is_reported_as_a_workflow_failure(
    agent: Callable[[Handler], TestClient],
) -> None:
    """没注入规则快照 = 输入缺失的失败：**不能**再返回 completed，但 error 必须透传。

    这正是"State 已经判定失败，API 必须如实表达"的最小验证 ——
    API 不重新判断业务（不看 rule_snapshot），只投影 State。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler))  # 刻意不带 snapshot

    assert response.status_code == 422, "失败就是失败，不能报 200"
    body = response.json()
    assert body["workflow_status"] == "rejected"
    assert body["error_code"] == "AGENT_INPUT_INVALID"
    assert "rule_snapshot" in body["error_message"]
    # 上传与解析本身是成功的，痕迹保留 —— 失败点不在它们身上
    assert body["contract_id"] == 11
    assert body["parse_result"]["status"] == "PARSED"


def test_empty_rule_set_is_completed_not_rejected(
    agent: Callable[[Handler], TestClient],
) -> None:
    """``rule_set_version=None, rules=[]``（该合同类型没配规则）= **正常完成**。

    与上一条的区别只在于"给了一个空快照"而不是"什么都没给"。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    empty = RuleSetSnapshot(contract_type="SERVICE", rule_set_version=None, rules=[])
    response = _post(agent(handler, snapshot=empty))

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_status"] == "completed"
    assert body["error_code"] is None


def test_empty_document_is_completed_not_rejected(
    agent: Callable[[Handler], TestClient],
) -> None:
    """空文档是**数据问题**（合同本身没内容），解析是成功的，不能说工作流失败。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler, snapshot=PURCHASE_SNAPSHOT), payload=docx_bytes())

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_status"] == "completed"
    assert body["parse_result"]["status"] == "EMPTY"
    assert body["parse_result"]["text"] == ""
    assert body["error_code"] is None


# --------------------------------------------------------------------------- #
# Backend 拒绝 —— Gate 拦下，不进入解析
# --------------------------------------------------------------------------- #
def test_backend_rejection_yields_422_and_no_parse(
    agent: Callable[[Handler], TestClient],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            415,
            json={"code": "UNSUPPORTED_FORMAT", "message": "不支持的文件扩展名", "details": {}},
        )

    response = _post(agent(handler))

    assert response.status_code == 422
    body = response.json()
    assert body["workflow_status"] == "rejected"
    # 保留 Backend 的稳定错误码
    assert body["error_code"] == "UNSUPPORTED_FORMAT"
    assert body["validation_errors"]
    assert body["parse_result"] is None, "被拦下时不得进入解析阶段"
    assert body["contract_id"] is None


# --------------------------------------------------------------------------- #
# Backend 不可达
# --------------------------------------------------------------------------- #
def test_backend_unreachable_yields_422(agent: Callable[[Handler], TestClient]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    response = _post(agent(handler))

    assert response.status_code == 422
    body = response.json()
    assert body["workflow_status"] == "rejected"
    assert body["error_code"] == "BACKEND_UNREACHABLE"
    assert body["parse_result"] is None


# --------------------------------------------------------------------------- #
# 临时文件生命周期
# --------------------------------------------------------------------------- #
def test_upload_temp_file_is_removed_after_the_request(
    agent: Callable[[Handler], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """无论工作流成败，请求内落盘的临时文件都必须被删除。"""
    created: list[Path] = []
    original = review_module._spool_to_temp

    async def spy(upload: Any, suffix: str) -> Path:
        path = await original(upload, suffix)
        created.append(path)
        return path

    monkeypatch.setattr(review_module, "_spool_to_temp", spy)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    response = _post(agent(handler, snapshot=PURCHASE_SNAPSHOT))

    assert response.status_code == 200
    assert len(created) == 1, "应当恰好落盘一个临时文件"
    assert not created[0].exists(), "请求结束后临时文件必须被删除"


def test_temp_file_is_also_removed_when_backend_rejects(
    agent: Callable[[Handler], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Path] = []
    original = review_module._spool_to_temp

    async def spy(upload: Any, suffix: str) -> Path:
        path = await original(upload, suffix)
        created.append(path)
        return path

    monkeypatch.setattr(review_module, "_spool_to_temp", spy)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(415, json={"code": "UNSUPPORTED_FORMAT", "message": "x"})

    response = _post(agent(handler))

    assert response.status_code == 422
    assert created and not created[0].exists(), "失败路径同样必须清理临时文件"


def test_agent_does_not_write_into_the_repo(
    agent: Callable[[Handler], TestClient],
) -> None:
    """Agent 不是存储层：它只用系统临时目录，绝不往仓库里写文件。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    _post(agent(handler))

    repo_root = Path(__file__).resolve().parents[3]
    agent_dir = repo_root / "ai-agent"
    stray = [p.name for p in agent_dir.rglob("agent-upload-*")]
    assert stray == [], f"Agent 目录下出现临时文件残留：{stray}"

    # 确认使用的确实是系统临时目录
    assert Path(tempfile.gettempdir()).is_dir()
