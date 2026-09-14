"""LLM Provider —— 只负责"怎么问、怎么收"（P9-1）。

职责边界
-------
::

    LLMRequest  ──▶  DeepSeekProvider.complete()  ──▶  LLMResult

**只做三件事**：拼 HTTP 请求、发出去、把响应原样封装成 :class:`LLMResult`。

**不做**：

* 不解析业务对象（不碰 Clause / Risk / 任何 finding schema）—— ``parsed`` 由调用方经
  :func:`~app.llm.json_guard.parse_and_validate` 回填
* 不判断响应内容是不是合法 JSON —— 那是 json_guard 的事（见 ``LLMResult.raw_text`` 的说明）
* 不认识 prompt 的业务内容 —— ``system_prompt`` / ``user_prompt`` 原样发出，
  只额外附加 json_guard 生成的**机械**结构说明
* 不自动重试、不做 JSON 修复（P9 明确不做，见 §9.1）

为什么用异常而不是返回值（与 ``upload_contract`` 的 ``UploadOutcome`` 不同）
--------------------------------------------------------------------
LLM 调用失败是**可降级**的：§9.1 第 4 道防线要求"本批降级为仅规则引擎结果并标记 warning"。
因此调用方**必须**处理它 —— 用异常强制这个处理动作出现在代码里看得见的地方，
而不是靠调用方记得检查一个可能被忽略的返回字段。
（规则集那边也是同一考虑，见 ``BackendRequestError``。）
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import httpx

from app.core.config import AgentSettings
from app.core.errors import AgentErrorCode
from app.llm.json_guard import build_schema_instruction
from app.llm.schemas import LLMRequest, LLMResult

logger = logging.getLogger(__name__)

#: 提供方标识（写进 ``LLMResult.provider`` 与将来的 ai_call_log）
PROVIDER_DEEPSEEK = "deepseek"

#: DeepSeek 的 OpenAI 兼容路径
_CHAT_COMPLETIONS = "/chat/completions"


class LLMUnavailableError(Exception):
    """LLM **没有给出可用回答**：未配置 / 连不上 / 超时 / 非 2xx / 响应体结构不对。

    这一类失败都是"我们没能问出一个结果"，与"问到了但内容不合契约"
    （:class:`~app.llm.json_guard.LLMSchemaInvalidError`）**必须分开**：
    前者可以重试或换 provider，后者要么改 prompt、要么丢这一条。

    :param status_code: HTTP 状态码；未发出请求时为 ``None``
    :param error_code: 取值见 :class:`~app.core.errors.AgentErrorCode`
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
        self.error_code = error_code or AgentErrorCode.LLM_UNAVAILABLE.value


class LLMProvider(Protocol):
    """LLM 提供方接口。

    目前只有 :class:`DeepSeekProvider` 一个实现 —— Protocol 的意义不在"多态"，
    而在**把契约钉死**：P9-2 的节点依赖这个形状，将来换模型时业务代码零改动
    （与架构文档 §9.1「LLMFactory.build(settings) 按配置切换」同一目标）。
    """

    async def complete(self, request: LLMRequest) -> LLMResult:
        """执行一次调用。失败时抛 :class:`LLMUnavailableError`。"""
        ...


class DeepSeekProvider:
    """DeepSeek（OpenAI 兼容接口）实现。

    :param settings: Agent 配置（读 ``deepseek_api_key`` / ``deepseek_base_url`` / ``deepseek_model``）
    :param client: 可注入的 ``httpx.AsyncClient``。测试传入挂了 ``httpx.MockTransport``
        的实例即可，**不需要真实调用模型**；注入的 client 由调用方负责关闭。
    """

    provider_name = PROVIDER_DEEPSEEK

    def __init__(self, settings: AgentSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    # ------------------------------ 生命周期 ------------------------------ #
    def _ensure_client(self) -> httpx.AsyncClient:
        """惰性创建 client —— 只有真正要发请求时才占用连接池资源。"""
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        """关闭 client。**只关闭自己创建的**；注入的由调用方负责。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -------------------------------- 调用 -------------------------------- #
    async def complete(self, request: LLMRequest) -> LLMResult:
        """调用 ``POST {base_url}/chat/completions``。

        **未配置时在本地短路**：key 或模型名为空就直接抛 :class:`LLMUnavailableError`，
        不发任何请求 —— 拿一个空 key 去请求只会换来一个 401，
        既浪费一次往返，也让"没配置"这个本地事实伪装成一个远端错误。
        """
        if not self._settings.llm_configured:
            raise LLMUnavailableError("LLM 未配置（缺少 DEEPSEEK_API_KEY 或 DEEPSEEK_MODEL），未发起任何请求")

        payload = self._build_payload(request)
        url = f"{self._settings.deepseek_base_url.rstrip('/')}{_CHAT_COMPLETIONS}"
        started = time.monotonic()

        try:
            response = await self._ensure_client().post(
                url,
                headers={
                    "Authorization": f"Bearer {self._settings.deepseek_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=request.timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"调用 LLM 失败（{url}）：{exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise LLMUnavailableError(
                f"LLM 返回 {response.status_code}：{_brief(response)}",
                status_code=response.status_code,
            )

        return self._to_result(response, latency_ms)

    # -------------------------------- 内部 -------------------------------- #
    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        """组装请求体 —— 第 1 道防线（协议层）在这里落地。

        ``response_format`` **只在要求结构化输出时带上**：它是"请给我 JSON"的硬约束，
        对不需要 JSON 的场景（如将来的摘要）加上去，等于把一个自由的回答硬掰成 JSON。
        """
        system_prompt = request.system_prompt
        payload: dict[str, Any] = {
            "model": self._settings.deepseek_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        if request.output_schema is not None:
            # 结构说明由 **同一个 Pydantic 模型**生成（单一事实来源，见 json_guard）
            payload["messages"] = [
                {
                    "role": "system",
                    "content": f"{system_prompt}\n\n{build_schema_instruction(request.output_schema)}",
                },
                {"role": "user", "content": request.user_prompt},
            ]
            payload["response_format"] = {"type": "json_object"}

        return payload

    def _to_result(self, response: httpx.Response, latency_ms: int) -> LLMResult:
        """响应 → :class:`LLMResult`（**原样搬运，不做内容判断**）。"""
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMUnavailableError(
                f"LLM 返回 {response.status_code}，但响应体不是 JSON", status_code=response.status_code
            ) from exc

        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise LLMUnavailableError(
                f"LLM 的响应体缺少 choices（{_brief(response)}）", status_code=response.status_code
            )

        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise LLMUnavailableError(
                f"LLM 的响应体里没有 message.content（{_brief(response)}）",
                status_code=response.status_code,
            )

        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return LLMResult(
            # parsed 刻意留空：Provider 不解析业务对象，由调用方经 json_guard 回填
            parsed=None,
            raw_text=content,
            model=str(body.get("model") or self._settings.deepseek_model),
            provider=self.provider_name,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
            request_id=str(body.get("id") or ""),
        )


def _brief(response: httpx.Response) -> str:
    """取一小段响应体用于报错 —— 绝不把整个响应塞进异常消息（可能很长，也可能含敏感内容）。"""
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - 取不到就算了，报错信息不值得再抛一次
        return "（响应体不可读）"
    text = " ".join(text.split())
    return text[:200] + ("…" if len(text) > 200 else "")


__all__ = ["PROVIDER_DEEPSEEK", "DeepSeekProvider", "LLMProvider", "LLMUnavailableError"]
