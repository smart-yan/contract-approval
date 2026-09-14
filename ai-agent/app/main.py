"""AI Agent 服务入口（FastAPI）。

定位（架构裁决）
----------------
* **Agent = 合同智能审查业务流程的核心**，LangGraph 是它的 Workflow Orchestrator。
* Agent **不访问数据库**，业务数据读写全部经 Backend 的领域 API。
* 人工审核**不属于** LangGraph —— 它由 Backend 承载，Agent 跑完即 END。

P5-2 阶段范围
-------------
本文件当前只提供**服务外壳与健康检查**。编排入口 ``POST /api/agent/review``
与最小 Graph（upload_file → validate_file → parse_document）属于 P5-3/P5-4。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

from app.core.config import get_settings

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
    logger.info("AI Agent 服务启动中 | %s", settings.safe_summary())
    yield
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


@app.get("/health", response_model=HealthResponse, tags=["infrastructure"], summary="健康检查")
async def health(request: Request, response: Response) -> HealthResponse:
    """Agent 自身健康检查。

    ⚠️ **P5 刻意不探测 Backend 的健康状态**：那需要一次出站 HTTP 调用，
    属于编排链路（P5-5 Tool）的职责，放在健康检查里会让两个服务的探针互相耦合。
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


__all__ = ["APP_VERSION", "HealthResponse", "app"]
