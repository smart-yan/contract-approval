"""健康检查端点（基础设施，**不属于 /api/v1 业务接口**）。

设计要点
--------
1. **不持有 Session，更不持有长事务。**
   数据库探测直接调用 ``app.db.session.check_connection()``，它内部是
   ``async with engine.connect()`` —— 从连接池借一条连接、执行两条只读 SQL、立即归还。
   全程没有创建 ``AsyncSession``，因此不存在"健康检查占着 Session/事务"的可能。

2. **有超时保护。** 数据库不可达时不能让 ``/health`` 长时间挂起：
   包一层 ``asyncio.timeout``，超时按"数据库不可用"处理。

3. **不泄露敏感信息。** 响应体只回传库名 / 版本 / 字符集等非敏感信息，
   **不含 DSN、不含用户名密码**（check_connection 返回的 `url` 字段刻意不放进响应）。

4. **状态语义**：全部检查通过 → 200 ``status=ok``；数据库不可用 → 503 ``status=degraded``。
   503 让编排系统（K8s readiness / 负载均衡）能把该实例摘掉。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.executors import executor_status
from app.core.logging import get_logger
from app.db.session import check_connection

logger = get_logger(__name__)

router = APIRouter(tags=["infrastructure"])

#: 健康检查里数据库探测的超时（秒）。比引擎的 connect_timeout 更短：
#: 健康检查宁可快速报"不健康"，也不该把一个探针请求挂住。
DB_PROBE_TIMEOUT_SECONDS: float = 3.0


class DatabaseCheck(BaseModel):
    """数据库探测结果。只包含非敏感字段。"""

    ok: bool
    database: str | None = None
    server_version: str | None = None
    charset: str | None = None
    latency_ms: float | None = None
    error_code: str | None = Field(default=None, description="失败时的业务错误码，如 DATABASE_UNAVAILABLE")
    error_type: str | None = Field(default=None, description="失败时的异常类型名，便于排查")


class ExecutorCheck(BaseModel):
    """并发执行器状态快照（只读，不触发创建）。"""

    thread_pool_created: bool
    thread_pool_size: int
    ocr_executor_created: bool
    ocr_executor_type: str | None = None
    ocr_executor_mode: str
    ocr_max_workers: int


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app_env: str
    version: str
    uptime_seconds: float | None = Field(
        default=None, description="自 lifespan 启动以来的秒数；未经 lifespan 时为空"
    )
    checks: dict[str, Any]


async def _probe_database() -> DatabaseCheck:
    """短生命周期地探测数据库。任何失败都收敛为 ``ok=False``，不向调用方抛异常。"""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(DB_PROBE_TIMEOUT_SECONDS):
            info = await check_connection()
    except TimeoutError:
        logger.warning("数据库健康探测超时", extra={"timeout_seconds": DB_PROBE_TIMEOUT_SECONDS})
        return DatabaseCheck(ok=False, error_code="PROBE_TIMEOUT", error_type="TimeoutError")
    except AppError as exc:
        # check_connection 已保证 message/details 脱敏，这里只取错误码与类型
        return DatabaseCheck(
            ok=False,
            error_code=str(exc.code),
            error_type=str(exc.details.get("error_type")) if exc.details else None,
        )

    return DatabaseCheck(
        ok=True,
        database=info["database"],
        server_version=info["server_version"],
        charset=info["charset"],
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="健康检查",
    responses={503: {"model": HealthResponse, "description": "存在不健康的依赖"}},
)
async def health(request: Request, response: Response) -> HealthResponse:
    """返回服务与依赖的健康状态。

    * 数据库不可用 → HTTP 503 + ``status=degraded``
    * 其余情况   → HTTP 200 + ``status=ok``
    """
    settings = get_settings()
    db_check = await _probe_database()

    started_at = getattr(request.app.state, "started_at", None)
    uptime = round(time.monotonic() - started_at, 3) if started_at is not None else None

    payload = HealthResponse(
        status="ok" if db_check.ok else "degraded",
        app_env=settings.app_env,
        version=request.app.version,
        uptime_seconds=uptime,
        checks={
            "database": db_check.model_dump(),
            "executors": ExecutorCheck(**executor_status()).model_dump(),
        },
    )

    if not db_check.ok:
        response.status_code = 503
        logger.warning(
            "健康检查未通过",
            extra={"error_code": db_check.error_code, "error_type": db_check.error_type},
        )

    return payload


__all__ = ["DatabaseCheck", "ExecutorCheck", "HealthResponse", "router"]
