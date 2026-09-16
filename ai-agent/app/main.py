"""AI Agent 服务入口（FastAPI）。

定位（架构裁决）
----------------
* **Agent = 合同智能审查业务流程的核心**，LangGraph 是它的 Workflow Orchestrator。
* Agent **不访问数据库**，业务数据读写全部经 Backend 的领域 API。
* 人工审核**不属于** LangGraph —— 它由 Backend 承载，Agent 跑完即 END。

当前范围（P5-2 ~ P5-4）
----------------------
* ``GET /health`` —— 服务健康检查
* ``POST /api/agent/review`` —— 编排入口，跑
  ``upload_file → validate_file →[Conditional Edge]→ parse_document``

**进程内只编译一次 Graph**：编译产物本身是无状态的，每次调用把初始 State 与
依赖（``ReviewContext``）传进去即可，因此可以安全地在多个并发请求间共享。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

from app.api import review_router
from app.background import BackgroundReviews
from app.core.config import get_settings
from app.graph.builder import build_review_graph
from app.llm.provider import DeepSeekProvider
from app.tools.backend_client import BackendClient

APP_VERSION = "0.1.0"

logger = logging.getLogger("agent")


def _configure_logging(level: str) -> None:
    """最简日志配置。

    Backend 有一套带 request_id 上下文的结构化日志；Agent 侧的日志体系
    属于后续阶段（接入 Graph 后节点级追踪才有意义），此处刻意保持最小。
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)

    # 进程级共享资源：一个 HTTP 连接池 + 一个 LLM provider + 一份编译好的 Graph。
    # 都是无状态/可复用的，放进 app.state 由 lifespan 统一管理生命周期，
    # 避免每个请求各自新建连接池。
    app.state.backend_client = BackendClient(
        settings.backend_base_url,
        timeout_seconds=settings.backend_timeout_seconds,
    )
    # LLM provider 在这里**无条件**创建：构造它不发任何请求，
    # 未配置密钥时 ``complete()`` 会在本地短路（见 DeepSeekProvider），
    # 于是图里的 ``llm_review`` 会走**降级**分支而不是报错。
    app.state.llm_provider = DeepSeekProvider(settings)
    app.state.review_graph = build_review_graph()
    # 后台审查的登记处（P14-4）：POST /review 返回 202 之后，图在这个进程里跑。
    # 它同时负责并发闸门与 shutdown 收尾 —— 见 app/background.py 的说明。
    app.state.background_reviews = BackgroundReviews()

    logger.info("AI Agent 服务启动中 | %s", settings.safe_summary())
    yield

    # ⚠️ 顺序要紧：**先等在跑的后台审查收尾**，再关连接池。
    # 反过来的话，还在跑的图会在"连接池已关闭"的客户端上发请求，
    # 抛出的是一个与真实原因无关的异常（`Cannot send a request, as the client
    # has been closed`），排查时完全看不出"其实是服务在关闭"。
    await app.state.background_reviews.drain()

    # 只关闭自己创建的资源；图是纯内存对象，无需释放
    await app.state.backend_client.aclose()
    await app.state.llm_provider.aclose()
    logger.info("AI Agent 服务关闭")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = Field(description="服务标识，用于区分 Agent 与 Backend")
    app_env: str
    version: str
    backend_base_url: str = Field(description="Agent 依赖的 Backend 基址（非敏感）")
    llm_configured: bool = Field(description="DeepSeek 配置是否齐全；**不回显密钥**")


app = FastAPI(
    title="合同审查 AI Agent",
    version=APP_VERSION,
    description="基于 LangGraph 的合同智能审查 Agent（业务流程核心）",
    lifespan=lifespan,
)

app.include_router(review_router)


@app.get("/health", response_model=HealthResponse, tags=["infrastructure"], summary="健康检查")
async def health(request: Request, response: Response) -> HealthResponse:
    """Agent 自身健康检查。

    ⚠️ **刻意不探测 Backend 的健康状态**：那需要一次出站 HTTP 调用，
    属于编排链路（``POST /api/agent/review``）的职责 ——
    放在健康检查里会让两个服务的探针互相耦合。
    此处只报告自身状态与配置是否就绪。
    """
    settings = get_settings()
    return HealthResponse(
        service="ai-agent",
        app_env=settings.app_env,
        version=request.app.version,
        backend_base_url=settings.backend_base_url,
        llm_configured=settings.llm_configured,
    )


__all__ = ["APP_VERSION", "HealthResponse", "app", "lifespan"]
