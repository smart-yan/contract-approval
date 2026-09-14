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
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app.api.review as review_module
from app.main import app
from app.tools.backend_client import BackendClient

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

#: 合法 DOCX 魔数（ZIP 头）—— Backend 侧才校验，这里只保证是一个可传输的字节流
DOCX_BYTES = b"PK\x03\x04 minimal docx fixture"

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]


@pytest.fixture
def agent() -> Iterator[Callable[[Handler], TestClient]]:
    """启动 Agent 应用，并把它的 ``BackendClient`` 换成挂了 MockTransport 的实例。

    ``app.state.backend_client`` 由 lifespan 创建；这里在启动后替换它，
    因此不需要 monkeypatch 全局、也不需要起真实 Backend ——
    这正是把依赖放进 ``app.state`` 而不是模块级单例的收益。
    """
    with ExitStack() as stack:
        opened: list[httpx.AsyncClient] = []

        def _make(handler: Handler) -> TestClient:
            test_client = stack.enter_context(TestClient(app))
            transport = httpx.MockTransport(handler)
            http_client = httpx.AsyncClient(transport=transport)
            opened.append(http_client)
            app.state.backend_client = BackendClient(BACKEND_BASE_URL, client=http_client)
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

    response = _post(agent(handler))

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

    # 3) 解析节点执行了，但只产出桩结果
    assert body["parse_result"]["status"] == "STUB"
    assert body["parse_result"]["source_file_type"] == "DOCX"
    assert body["parse_result"]["text"] == ""
    assert body["validation_errors"] == []
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

    response = _post(agent(handler))

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
