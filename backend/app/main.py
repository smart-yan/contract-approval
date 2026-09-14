"""FastAPI 应用入口（架构文档 §2.1 main.py）。

职责边界
--------
本模块只做**装配**：实例化、生命周期、中间件、异常处理器、挂载路由。
不含任何业务逻辑，也不在 P2-d 阶段挂载 ``/api/v1`` 业务路由（那是 P4 起的事）。

生命周期（lifespan）
--------------------
启动顺序与关闭顺序**严格对称**，且都对异常保持健壮：

    启动                                 关闭
    ├─ setup_logging()                   ├─ shutdown_executors()
    ├─ install_default_executor()        └─ dispose_engine()
    └─ check_connection()（探测，不致命）

* **启动失败不阻断启动**：数据库暂时不可用是瞬时故障，此时拒绝启动会让
  ``/health`` 失去意义（服务都没起来，怎么报告"数据库挂了"）。
  因此探测失败只记 warning，服务照常启动，由 ``/health`` 返回 503 对外表达。
* **关闭必须释放连接池**：``dispose_engine()`` 关闭引擎并清空模块状态；
  ``shutdown_executors()`` 关闭线程池 / OCR 执行器。
  两者都是幂等的，重复关闭不会报错。

异常处理（P2-d 只做「基座接入」）
---------------------------------
* ``AppError`` → 按其 ``http_status`` 返回 ``to_dict()``（含 error code 与 request_id）；
* 其它未捕获异常 → 统一 500 + ``INTERNAL_ERROR``，**不回传异常原文**（避免泄露内部细节）。

⚠️ **统一响应体（成功响应也包 ``{code, message, data}``）与分页属于 P4**，本阶段不做。

请求上下文
----------
中间件为每个请求生成 / 透传 ``X-Request-ID``，写入日志上下文，
使该请求内的所有日志（含异常日志）都能通过 request_id 串联，
并让 ``AppError.to_dict()`` 里的 ``request_id`` 字段真正有值。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.executors import install_default_executor, shutdown_executors
from app.core.logging import bind_context, clear_context, get_logger, setup_logging
from app.db.session import check_connection, dispose_engine

logger = get_logger(__name__)

#: 与 pyproject.toml 的 project.version 保持一致
APP_VERSION = "0.1.0"

#: 请求 ID 头：有则透传（便于跨服务追踪），无则生成
REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：管理日志、执行器与数据库引擎的创建与释放。"""
    # ---------------------------- 启动 ---------------------------- #
    setup_logging()
    settings = get_settings()
    logger.info("应用启动中", extra={"app_env": settings.app_env, "version": APP_VERSION})

    # 把事件循环的默认执行器换成受控线程池（asyncio.to_thread 之后走它）
    await install_default_executor()

    # 数据库连通性探测。刻意**不因失败而拒绝启动** —— 见模块 docstring。
    try:
        info = await check_connection()
        logger.info(
            "数据库连接正常",
            extra={
                "database": info["database"],
                "server_version": info["server_version"],
                "charset": info["charset"],
            },
        )
    except AppError as exc:
        logger.warning(
            "数据库当前不可用，服务仍将启动，/health 会返回 503",
            extra={"error_code": str(exc.code)},
        )

    # 记录启动时刻，供 /health 计算 uptime
    app.state.started_at = time.monotonic()

    yield

    # ---------------------------- 关闭 ---------------------------- #
    logger.info("应用关闭中")
    # 顺序与启动相反：先停执行器（不再接受新任务），再释放数据库连接池
    shutdown_executors()
    await dispose_engine()
    logger.info("应用已关闭")


async def request_context_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """为每个请求绑定 ``request_id`` 日志上下文，并记录访问日志。

    ``clear_context()`` 必须执行 —— 否则在复用协程/线程池的场景下，
    上一个请求的 request_id 会串到下一个请求的日志里。
    """
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
    bind_context(request_id=request_id)

    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        logger.info(
            "请求完成",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        clear_context()


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """业务异常 → 按其自带的状态码与脱敏后的错误体返回。"""
    logger.warning(
        "业务异常",
        extra={
            "error_code": str(exc.code),
            "error_category": str(exc.category),
            "http_status": exc.http_status,
            "path": request.url.path,
        },
    )
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """未预期异常 → 统一 500，**不回传异常原文**。"""
    logger.exception(
        "未处理异常",
        extra={"path": request.url.path, "error_type": type(exc).__name__},
    )
    fallback = AppError(code=ErrorCode.INTERNAL_ERROR)
    return JSONResponse(status_code=fallback.http_status, content=fallback.to_dict())


def create_app() -> FastAPI:
    """构造 FastAPI 应用。

    做成工厂函数而不是只有模块级单例，是为了让测试可以构造独立实例
    （例如挂一条会抛异常的临时路由来验证异常处理器）。
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=APP_VERSION,
        description="企业合同审批审查系统 - 后端服务",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        # P4 起再挂 /api/v1 业务路由
    )

    # 基础设施端点（健康检查不属于业务接口，故不加 /api/v1 前缀）
    app.include_router(health_router)

    app.middleware("http")(request_context_middleware)

    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)  # type: ignore[arg-type]

    return app


#: uvicorn 入口：``uvicorn app.main:app``
app = create_app()
