"""Backend 领域 API 的 HTTP 客户端 —— Agent 的 "Tool 层" 雏形。

职责边界（架构裁决）
------------------
Backend 在 P4 已经实现了文件类型校验（扩展名 / MIME / 魔数）、SHA256、
幂等（sha256 去重）、并发竞争处理与孤儿清理。**Agent 不重新实现其中任何一项**，
它只做两件事：

1. 把文件与业务元数据 POST 给 ``POST /api/v1/contracts``
2. 把 Backend 的响应 / 错误翻译成 State 能承载的结果

P8-2 起它多一个职责：**把规则目录拉回来**（``GET /api/v1/rule-sets``）。
注意这里**只做传输**：响应体原样交给 ``app/rules/catalog.py`` 解释 ——
本模块不认识 ``AgentRule``，也不求值（规则数据归 Backend、求值引擎归 Agent）。

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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.errors import AgentErrorCode

#: Backend 的业务 API 前缀（与 ``backend/app/core/config.py`` 的 api_v1_prefix 对应）
API_V1_PREFIX = "/api/v1"

#: 201 = 新建成功；200 = 命中 sha256 复用了已有记录。两者都算成功。
_SUCCESS_STATUS: frozenset[int] = frozenset({200, 201})


class BackendRequestError(Exception):
    """一次 Backend 请求失败：连不上 / 超时 / 非 2xx / 响应体不是 JSON 对象。

    为什么规则集用异常，而上传用 :class:`UploadOutcome` 返回值
    -------------------------------------------------------
    ``upload_contract`` 是一次**业务动作**：被拒绝（格式不支持、超过大小上限）
    也是一种要展示给用户的结果，所以它连同成功信息一起返回，由节点决定怎么写进 State。

    规则集不一样：它是**后续所有求值的前提**。取不到就没有任何可继续的余地，
    也不存在"部分成功" —— 调用方必须处理它。用异常是为了让这个处理动作
    **强制出现在代码里看得见的地方**，而不是靠调用方记得检查一个返回字段。

    ⚠️ 这不违背"失败写进 State、由 Conditional Edge 分流"：异常只在**没被接住**时向上冒。
    接住它并翻译成 State 是 P8-2 节点的事 —— 本层只负责把失败**说清楚**。

    :param status_code: Backend 的 HTTP 状态码；连不上时为 ``None``
    :param error_code: Backend 统一错误体里的 ``code``，或 :class:`AgentErrorCode` 取值
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


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


@dataclass(frozen=True, slots=True)
class PersistOutcome:
    """一次"把 Agent 的产出整批写回某个任务"的调用结果。

    覆盖 :meth:`BackendClient.persist_task_risks`（P9-10）与
    :meth:`BackendClient.persist_task_document`（P10-3）——
    两者都是"向任务下的某个批量写接口 POST 一批已映射好的条目"，
    传输层关心的东西完全一样（状态码 / 响应体 / 错误码），
    因此**共用**这一个形状。

    与 :class:`UploadOutcome` 仍然分开：那个的 ``payload`` 是
    ``ContractIngestResponse``，且它内部还要处理文件流，形态本就不同。
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

    # ------------------------------ 规则目录 ------------------------------ #
    async def get_effective_rule_set(self, contract_type: str) -> dict[str, Any]:
        """调用 ``GET /api/v1/rule-sets``，返回**原始响应体**（dict）。

        职责只有 transport 这一段：**HTTP → Python 结构**。
        2xx + JSON 对象 → 原样返回；其余一律抛 :class:`BackendRequestError`。
        **不做任何领域解释** —— 响应体里有什么规则、``rule_set=null`` 意味着什么，
        都由 ``app/rules/catalog.py`` 的 ``snapshot_from_backend`` 回答。
        这里不认识 ``AgentRule``，不映射字段，不求值。

        "该合同类型没有启用的规则集"是 **200 + ``rule_set=null``**，
        因此它是一个**成功**的返回值，不是错误。
        """
        client = self._ensure_client()

        try:
            response = await client.get(
                f"{self._api_base}/rule-sets",
                params={"contract_type": contract_type},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise BackendRequestError(
                f"调用 Backend 读取规则集失败：{exc}",
                error_code=AgentErrorCode.BACKEND_UNREACHABLE.value,
            ) from exc

        if response.status_code not in _SUCCESS_STATUS:
            error_code, error_message = _extract_error(response)
            raise BackendRequestError(
                f"Backend 拒绝读取规则集（HTTP {response.status_code}）：{error_message}",
                status_code=response.status_code,
                error_code=error_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendRequestError(
                f"Backend 返回 {response.status_code}，但响应体不是 JSON",
                status_code=response.status_code,
                error_code=AgentErrorCode.BACKEND_REJECTED.value,
            ) from exc

        if not isinstance(payload, dict):
            # 和 upload_contract 同一口径：2xx 但形状不对，属于契约被破坏
            raise BackendRequestError(
                "Backend 的规则集响应不是 JSON 对象",
                status_code=response.status_code,
                error_code=AgentErrorCode.BACKEND_REJECTED.value,
            )
        return payload

    # ------------------------------ 风险写入 ------------------------------ #
    async def persist_task_risks(
        self,
        task_id: int,
        *,
        risks: Sequence[dict[str, Any]],
    ) -> PersistOutcome:
        """调用 ``POST /api/v1/review-tasks/{task_id}/risks``，把整批风险写进 Backend。

        :param risks: **已经映射好**的请求体条目（映射在
            ``app/tools/risk_persistence.py`` —— 本层只做 transport，
            不认识 :class:`~app.risk.schemas.AgentRiskItem`）

        本方法**不抛业务异常**：所有失败都翻译成 :class:`PersistOutcome`，
        由调用它的节点写进 State。与 ``upload_contract`` 同一口径 ——
        被拒绝（任务已写入过、规则编码解析不出来）也是一种要如实上报的结果，
        而不是需要在每一层都显式接住的异常。

        ``409 TASK_ALREADY_PERSISTED`` 在这里**不是特殊分支**：它按普通失败处理，
        ``error_code`` 原样带回 Backend 的错误码。
        """
        client = self._ensure_client()

        try:
            response = await client.post(
                f"{self._api_base}/review-tasks/{task_id}/risks",
                json={"risks": list(risks)},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return PersistOutcome(
                ok=False,
                status_code=None,
                payload=None,
                error_code=AgentErrorCode.BACKEND_UNREACHABLE.value,
                error_message=f"调用 Backend 写入风险失败：{exc}",
            )

        if response.status_code in _SUCCESS_STATUS:
            try:
                payload = response.json()
            except ValueError:
                return PersistOutcome(
                    ok=False,
                    status_code=response.status_code,
                    payload=None,
                    error_code=AgentErrorCode.BACKEND_REJECTED.value,
                    error_message=f"Backend 返回 {response.status_code}，但响应体不是 JSON",
                )
            if not isinstance(payload, dict):
                return PersistOutcome(
                    ok=False,
                    status_code=response.status_code,
                    payload=None,
                    error_code=AgentErrorCode.BACKEND_REJECTED.value,
                    error_message="Backend 的成功响应不是 JSON 对象",
                )
            return PersistOutcome(
                ok=True,
                status_code=response.status_code,
                payload=payload,
                error_code=None,
                error_message=None,
            )

        error_code, error_message = _extract_error(response)
        return PersistOutcome(
            ok=False,
            status_code=response.status_code,
            payload=None,
            error_code=error_code,
            error_message=error_message,
        )

    # ------------------------------ 文档写入 ------------------------------ #
    async def persist_task_document(
        self,
        task_id: int,
        *,
        parse_status: str,
        blocks: Sequence[dict[str, Any]],
        clauses: Sequence[dict[str, Any]],
        metadata: Sequence[dict[str, Any]],
    ) -> PersistOutcome:
        """调用 ``POST /api/v1/review-tasks/{task_id}/document``，把文档层结果写回 Backend。

        :param parse_status: ``PARSED`` / ``FAILED``（Backend 只收这两个终态）
        :param blocks / clauses / metadata: **已经映射好**的请求体条目
            （映射在 ``app/tools/document_persistence.py`` —— 本层只做 transport，
            不认识 :class:`~app.schemas.document.ParseResult` / ``Clause`` / ``MetadataItem``）

        本方法**不抛业务异常**：所有失败都翻译成 :class:`PersistOutcome`，
        与 ``persist_task_risks`` 同一口径。

        ⚠️ 三张表在 Backend 侧是**一个事务**：``clause`` / ``metadata`` 引用本次写入的
        ``document_block.id``（由 Backend 按请求里的下标解析）。因此这里必须**一次调用**
        把三者一起发出去，不能拆。
        """
        client = self._ensure_client()

        try:
            response = await client.post(
                f"{self._api_base}/review-tasks/{task_id}/document",
                json={
                    "parse_status": parse_status,
                    "blocks": list(blocks),
                    "clauses": list(clauses),
                    "metadata": list(metadata),
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return PersistOutcome(
                ok=False,
                status_code=None,
                payload=None,
                error_code=AgentErrorCode.BACKEND_UNREACHABLE.value,
                error_message=f"调用 Backend 写入文档层失败：{exc}",
            )

        if response.status_code in _SUCCESS_STATUS:
            try:
                payload = response.json()
            except ValueError:
                return PersistOutcome(
                    ok=False,
                    status_code=response.status_code,
                    payload=None,
                    error_code=AgentErrorCode.BACKEND_REJECTED.value,
                    error_message=f"Backend 返回 {response.status_code}，但响应体不是 JSON",
                )
            if not isinstance(payload, dict):
                return PersistOutcome(
                    ok=False,
                    status_code=response.status_code,
                    payload=None,
                    error_code=AgentErrorCode.BACKEND_REJECTED.value,
                    error_message="Backend 的成功响应不是 JSON 对象",
                )
            return PersistOutcome(
                ok=True,
                status_code=response.status_code,
                payload=payload,
                error_code=None,
                error_message=None,
            )

        error_code, error_message = _extract_error(response)
        return PersistOutcome(
            ok=False,
            status_code=response.status_code,
            payload=None,
            error_code=error_code,
            error_message=error_message,
        )


__all__ = [
    "API_V1_PREFIX",
    "BackendClient",
    "BackendRequestError",
    "PersistOutcome",
    "UploadOutcome",
]
