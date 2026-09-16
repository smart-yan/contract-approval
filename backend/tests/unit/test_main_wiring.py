"""app/main.py 装配层单元测试（**不需要数据库**）。

这些用例刻意不进入 lifespan（不使用 ``with TestClient(...)``），
并把健康检查依赖的数据库探测替换成桩，从而与真实数据库解耦 ——
真正走 lifespan 与真实数据库的用例在 ``tests/integration/test_health_endpoint.py``。

覆盖：应用元信息、路由挂载、request_id 中间件、AppError 与未捕获异常的处理器。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.health as health_module
from app.core.errors import AppError, ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_context
from app.main import APP_VERSION, REQUEST_ID_HEADER, create_app


@pytest.fixture
def app() -> FastAPI:
    """构造一个带临时探针路由的应用实例，用于验证异常处理器。"""
    application = create_app()

    @application.get("/__probe__/app-error")
    async def _raise_app_error() -> None:
        raise NotFoundError("合同 42 不存在", details={"contract_id": 42})

    @application.get("/__probe__/encrypted")
    async def _raise_blocked() -> None:
        raise AppError(
            code=ErrorCode.FILE_ENCRYPTED,
            details={"file_name": "secret.pdf"},
        )

    @application.get("/__probe__/boom")
    async def _raise_unexpected() -> None:
        raise ValueError("internal detail that must never leak")

    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # 不使用 with：不触发 lifespan（避免连接真实数据库）
    # raise_server_exceptions=False：未捕获异常要返回 500 响应体，而不是在测试里重新抛出
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# 应用装配
# --------------------------------------------------------------------------- #
def test_create_app_sets_metadata() -> None:
    application = create_app()
    assert application.version == APP_VERSION
    assert application.title
    assert application.docs_url == "/docs"


def test_health_route_stays_at_root_not_under_api_prefix(app: FastAPI) -> None:
    """健康检查是基础设施端点，始终挂在根路径，不随业务路由一起进 /api/v1。

    刻意通过 OpenAPI schema 内省路径，而不是遍历 ``app.routes``：
    新版 FastAPI/Starlette 的 ``include_router`` 会把子路由包成 ``_IncludedRouter``
    对象（既没有 ``.path`` 也不平铺进 ``app.routes``），遍历 ``app.routes`` 会漏掉
    ``/health``。schema 反映的是**真正对外提供的路径**，不随实现细节变化。
    """
    paths = set(app.openapi()["paths"])
    assert "/health" in paths
    assert "/api/v1/health" not in paths


def test_only_approved_business_routes_are_mounted(app: FastAPI) -> None:
    """``/api/v1`` 下的路由白名单守卫。

    【本用例的前身是 P2-d 的 ``test_health_route_is_mounted_without_api_prefix``】
    它当时断言 ``/api/v1`` 下**一条路由都没有**（P2-d 不实现业务路由）。
    P4 引入了第一个已批准的业务路由，该断言必然失效——按守卫的本意改写为
    "**只允许已批准的路由出现**"，而不是删掉测试。

    新增业务端点时**必须同步修改此白名单**：这样"未经裁决就加路由"会直接在 CI 暴露。

    白名单演进：
    * P4    ``/api/v1/contracts``  —— 合同接入
    * P8-0  ``/api/v1/rule-sets``  —— 规则读取（只读，给 Agent 取规则用）
    * P9-10 ``/api/v1/review-tasks/{task_id}/risks`` —— 风险写入
      （Agent 合并后的最终风险整批落库，并把任务置为已审查）
    * P10-1 ``/api/v1/review-tasks/{task_id}/document`` —— 文档层写入
      （块 / 条款 / 元数据整批落库，并更新附件解析状态与任务阶段）
    * P11-4 ``/api/v1/review-tasks/{task_id}/workbench`` —— 工作台查询
      （一次取齐合同 / 原文 / 条款 / 元数据 / 风险；P11-3 的 ``/api/v1/contracts``
      是同一个路径上新增的 GET，白名单按**路径**比较，因此不受影响）
    * P12-3 ``/api/v1/review-tasks/{task_id}/report/export`` —— 报告导出
      （只读投影 → Markdown，无副作用；不建 ``report`` 表，因此没有第二个路由）
    * P13-1 ``/api/v1/review-tasks/{task_id}/risks/{risk_id}`` —— 人工复核
      （PATCH 一条已复核结论。⚠️ 它与上面的 ``.../risks`` 是**两个不同的路径**，
      不是同一个路径上的新方法 —— 白名单按路径比较，因此必须单独列出）
    * P14-4 ``/api/v1/review-tasks/{task_id}/block`` —— 任务阻塞写入
      （Agent 后台执行失败时如实上报；**只写 status 与 block_reason_\\***）
    """
    approved = {
        "/api/v1/contracts",
        "/api/v1/rule-sets",
        "/api/v1/review-tasks/{task_id}/risks",
        "/api/v1/review-tasks/{task_id}/document",
        "/api/v1/review-tasks/{task_id}/workbench",
        "/api/v1/review-tasks/{task_id}/report/export",
        "/api/v1/review-tasks/{task_id}/risks/{risk_id}",
        "/api/v1/review-tasks/{task_id}/block",
    }

    paths = set(app.openapi()["paths"])
    mounted = {path for path in paths if path.startswith("/api/v1")}

    assert mounted == approved, (
        f"未批准的 /api/v1 路由：{mounted - approved}；缺失的已批准路由：{approved - mounted}"
    )


def test_create_app_returns_independent_instances() -> None:
    """工厂函数语义：两次调用得到两个独立应用。"""
    assert create_app() is not create_app()


# --------------------------------------------------------------------------- #
# request_id 中间件
# --------------------------------------------------------------------------- #
def test_request_id_is_generated_and_echoed(client: TestClient) -> None:
    response = client.get("/__probe__/app-error")
    request_id = response.headers.get(REQUEST_ID_HEADER)
    assert request_id, "中间件必须回写 request_id 响应头"
    assert len(request_id) == 32  # uuid4().hex


def test_inbound_request_id_is_preserved(client: TestClient) -> None:
    """上游传入的 request_id 要透传，便于跨服务追踪。"""
    upstream = "trace-from-upstream-001"
    response = client.get("/__probe__/app-error", headers={REQUEST_ID_HEADER: upstream})
    assert response.headers[REQUEST_ID_HEADER] == upstream


async def test_request_id_does_not_leak_between_concurrent_requests() -> None:
    """并发隔离：多个请求同时在飞时，各自的 request_id 绝不能串。

    这是 ``bind_context`` 使用 ``contextvars`` 而非全局字典的**核心理由** ——
    ``BaseHTTPMiddleware`` 会把下游应用放进子任务执行，每个请求一个任务、
    各自持有一份上下文副本。若哪天有人把它改成模块级变量，这条用例会立刻失败。

    用 ``httpx.ASGITransport`` 而不是 ``TestClient``：后者是同步的，
    没法在同一个事件循环里真正并发地发起请求 —— 而"同一个事件循环内的并发"
    恰恰是 contextvars 隔离性唯一会被考验的场景。
    """
    application = create_app()

    @application.get("/__probe__/echo-request-id")
    async def _echo_request_id() -> dict[str, str | None]:
        # 让出控制权，强制多个请求在事件循环里交错执行
        await asyncio.sleep(0.01)
        return {"request_id": get_context().get("request_id")}

    @application.get("/__probe__/concurrent-error")
    async def _raise_concurrently() -> None:
        await asyncio.sleep(0.01)
        raise NotFoundError("并发隔离探针")

    request_count = 10
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            *(
                client.get(
                    "/__probe__/echo-request-id" if index % 2 == 0 else "/__probe__/concurrent-error",
                    headers={REQUEST_ID_HEADER: f"req-{index}"},
                )
                for index in range(request_count)
            )
        )

    seen = []
    for index, response in enumerate(responses):
        expected = f"req-{index}"
        # 成功响应看响应头，错误响应看错误体（错误体里的 request_id 直接取自日志上下文）
        assert response.headers[REQUEST_ID_HEADER] == expected
        assert response.json()["request_id"] == expected, (
            f"第 {index} 个请求拿到的 request_id 是 {response.json()['request_id']!r}，"
            f"期望 {expected!r} —— 并发请求之间发生了上下文串扰"
        )
        seen.append(response.json()["request_id"])

    assert len(set(seen)) == request_count, "并发请求的 request_id 出现了重复"

    # 请求全部结束后上下文必须已被清空，不能残留到下一个使用方
    assert get_context() == {}


# --------------------------------------------------------------------------- #
# 异常处理器
# --------------------------------------------------------------------------- #
def test_app_error_maps_to_its_http_status_and_body(client: TestClient) -> None:
    response = client.get("/__probe__/app-error")
    assert response.status_code == 404

    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["message"] == "合同 42 不存在"
    assert body["details"] == {"contract_id": 42}


def test_request_id_flows_into_error_body(client: TestClient) -> None:
    """中间件绑定的 request_id 必须出现在错误体里（否则日志无法与用户报错对齐）。"""
    response = client.get("/__probe__/app-error")
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_app_error_keeps_its_error_code_semantics(client: TestClient) -> None:
    """加密文件 → 422 / USER_ERROR，与 §13.1 的分类保持一致。"""
    response = client.get("/__probe__/encrypted")
    assert response.status_code == 422
    assert response.json()["code"] == "FILE_ENCRYPTED"


def test_unhandled_exception_returns_generic_500_without_leaking(client: TestClient) -> None:
    response = client.get("/__probe__/boom")
    assert response.status_code == 500

    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert "internal detail" not in body["message"], "未捕获异常的原文不得回传"
    assert "ValueError" not in str(body)


def test_validation_error_subclass_is_handled(client: TestClient, app: FastAPI) -> None:
    @app.get("/__probe__/validation")
    async def _raise_validation() -> None:
        raise ValidationError("参数不合法")

    response = client.get("/__probe__/validation")
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# /health（用桩替换数据库探测，不依赖真实数据库）
# --------------------------------------------------------------------------- #
def test_health_reports_ok_when_database_is_reachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_probe():
        return health_module.DatabaseCheck(
            ok=True,
            database="contract_approval",
            server_version="8.4.8",
            charset="utf8mb4",
            latency_ms=1.23,
        )

    monkeypatch.setattr(health_module, "_probe_database", _fake_probe)

    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["database"]["database"] == "contract_approval"
    assert body["checks"]["executors"]["ocr_executor_created"] is False


def test_health_returns_503_when_database_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _failing_probe():
        return health_module.DatabaseCheck(
            ok=False, error_code="DATABASE_UNAVAILABLE", error_type="OperationalError"
        )

    monkeypatch.setattr(health_module, "_probe_database", _failing_probe)

    response = client.get("/health")
    assert response.status_code == 503

    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["database"]["error_code"] == "DATABASE_UNAVAILABLE"


def test_health_never_exposes_credentials(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """响应体里不允许出现 DSN / 用户名 / 密码。"""
    from app.core.config import get_settings

    settings = get_settings()

    async def _fake_probe():
        return health_module.DatabaseCheck(ok=True, database=settings.mysql_db, charset="utf8mb4")

    monkeypatch.setattr(health_module, "_probe_database", _fake_probe)

    raw = client.get("/health").text
    assert "://" not in raw, "健康检查不得回传连接串"
    assert settings.mysql_password.get_secret_value() not in raw
    assert "mysql" not in raw.lower()


def test_health_reports_degraded_on_probe_timeout(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探测超时必须收敛为"不健康"，而不是让请求挂住。"""

    async def _timeout_probe():
        return health_module.DatabaseCheck(ok=False, error_code="PROBE_TIMEOUT", error_type="TimeoutError")

    monkeypatch.setattr(health_module, "_probe_database", _timeout_probe)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["error_code"] == "PROBE_TIMEOUT"


def test_health_uptime_is_none_without_lifespan(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """未经过 lifespan 时 started_at 不存在，uptime 应为空而不是编造一个值。"""

    async def _fake_probe():
        return health_module.DatabaseCheck(ok=True, charset="utf8mb4")

    monkeypatch.setattr(health_module, "_probe_database", _fake_probe)
    response = client.get("/health")
    assert response.json()["uptime_seconds"] is None
