"""Backend 领域 API 的 HTTP 客户端 —— Agent 的 "Tool 层" 雏形。

职责边界（架构裁决）
------------------
Backend 在 P4 已经实现了文件类型校验（扩展名 / MIME / 魔数）、SHA256、
幂等（sha256 去重）、并发竞争处理与孤儿清理。**Agent 不重新实现其中任何一项**，
它只做两件事：

1. 把文件与业务元数据 POST 给 ``POST /api/v1/contracts``
2. 把 Backend 的响应 / 错误翻译成 State 能承载的结果

P5-5 引入 Tool Calling 时，会把这里的方法包装成 LangChain Tool，
但**网络实现不变** —— 这也是把 HTTP 细节单独放一层的原因。

为什么不直接用 SQLAlchemy
-----------------------
Agent 与 Backend 是两个服务，边界是 HTTP。一旦 Agent 直连数据库，
Backend 的幂等与并发控制就被绕过了，两边的业务规则也会开始漂移。

流式上传
-------
与 Backend 侧一致，把文件对象交给 httpx 分块读取，避免把整个文件读进内存。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.errors import AgentErrorCode

#: Backend 的业务 API 前缀（与 ``backend/app/core/config.py`` 的 api_v1_prefix 对应）
API_V1_PREFIX = "/api/v1"

#: 201 = 新建成功；200 = 命中 sha256 复用了已有记录。两者都算成功。
_SUCCESS_STATUS: frozenset[int] = frozenset({200, 201})


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """一次 :meth:`BackendClient.upload_contract` 调用的结果。

    ``ok=True`` 时 ``payload`` 是 Backend 的 ``ContractIngestResponse`` 字典；
    ``ok=False`` 时 ``error_code`` / ``error_message`` 有值。
    """

    ok: bool
    status_code: int | None
    payload: dict[str, Any] | None
    error_code: str | None
    error_message: str | None


def _extract_error(response: httpx.Response) -> tuple[str, str]:
    """从 Backend 的统一错误响应体 ``{code, message, details, request_id}`` 提取错误。

    提取不到就退回 :attr:`AgentErrorCode.BACKEND_REJECTED` ——
    不能因为对方返回了一页 HTML 错误页，就把整个结果丢掉。
    """
    try:
        body = response.json()
    except ValueError:
        return (
            AgentErrorCode.BACKEND_REJECTED.value,
            f"Backend 返回 {response.status_code}，响应体不是 JSON",
        )

    if isinstance(body, dict) and isinstance(body.get("code"), str):
        message = body.get("message")
        return body["code"], message if isinstance(message, str) else ""

    return AgentErrorCode.BACKEND_REJECTED.value, f"Backend 返回 {response.status_code}"


class BackendClient:
    """Backend 领域 API 客户端。

    :param base_url: Backend 基址，如 ``http://127.0.0.1:8000``（**不含** ``/api/v1``）
    :param timeout_seconds: 请求超时
    :param client: 可注入的 ``httpx.AsyncClient``。测试传入一个挂了
        ``httpx.MockTransport`` 的实例即可，无需启动真实 Backend；
        注入的 client 由调用方负责关闭。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_base = f"{base_url.rstrip('/')}{API_V1_PREFIX}"
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None

    # ------------------------------ 生命周期 ------------------------------ #
    def _ensure_client(self) -> httpx.AsyncClient:
        """惰性创建 client —— 只有真正要发请求时才占用连接池资源。"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """关闭 client。**只关闭自己创建的**；注入的由调用方负责。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -------------------------------- 上传 -------------------------------- #
    async def upload_contract(
        self,
        *,
        file_path: Path,
        filename: str,
        contract_no: str,
        title: str,
        contract_type: str,
        content_type: str | None = None,
    ) -> UploadOutcome:
        """调用 ``POST /api/v1/contracts`` 完成合同接入。

        本方法**不抛业务异常**：所有失败都翻译成 :class:`UploadOutcome`，
        由调用它的节点写进 State。
        """
        client = self._ensure_client()

        try:
            fp = await asyncio.to_thread(file_path.open, "rb")
        except OSError as exc:
            return UploadOutcome(
                ok=False,
                status_code=None,
                payload=None,
                error_code=AgentErrorCode.AGENT_INPUT_INVALID.value,
                error_message=f"无法读取待上传文件：{exc}",
            )

        try:
            response = await client.post(
                f"{self._api_base}/contracts",
                files={"file": (filename, fp, content_type or "application/octet-stream")},
                data={
                    "contract_no": contract_no,
                    "title": title,
                    "contract_type": contract_type,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # 连不上 / 超时 / 协议错误：P6 的 ErrorClassifier 会把这类归为 SYSTEM_ERROR
            return UploadOutcome(
                ok=False,
                status_code=None,
                payload=None,
                error_code=AgentErrorCode.BACKEND_UNREACHABLE.value,
                error_message=f"调用 Backend 失败：{exc}",
            )
        finally:
            await asyncio.to_thread(fp.close)

        if response.status_code in _SUCCESS_STATUS:
            try:
                payload = response.json()
            except ValueError:
                return UploadOutcome(
                    ok=False,
                    status_code=response.status_code,
                    payload=None,
                    error_code=AgentErrorCode.BACKEND_REJECTED.value,
                    error_message=f"Backend 返回 {response.status_code}，但响应体不是 JSON",
                )
            if not isinstance(payload, dict):
                return UploadOutcome(
                    ok=False,
                    status_code=response.status_code,
                    payload=None,
                    error_code=AgentErrorCode.BACKEND_REJECTED.value,
                    error_message="Backend 成功响应不是 JSON 对象",
                )
            return UploadOutcome(
                ok=True,
                status_code=response.status_code,
                payload=payload,
                error_code=None,
                error_message=None,
            )

        error_code, error_message = _extract_error(response)
        return UploadOutcome(
            ok=False,
            status_code=response.status_code,
            payload=None,
            error_code=error_code,
            error_message=error_message,
        )


__all__ = ["API_V1_PREFIX", "BackendClient", "UploadOutcome"]
