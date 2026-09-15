"""``DeepSeekProvider`` —— 只验证"怎么问、怎么收"。

只 mock **网络层**（``httpx.MockTransport``），**绝不真实调用 DeepSeek**：
Provider 的职责是传输与封装，把 HTTP 打桩之后，它剩下的每一行都真实执行。

边界也在这里钉死：Provider **不解析业务对象、不判断内容合法性** ——
"返回了一段不是 JSON 的文本"照样原样收下，判断交给 ``json_guard``。
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.core.config import AgentSettings
from app.core.constants import LLMScene
from app.core.errors import AgentErrorCode
from app.llm.json_guard import LLMSchemaInvalidError, parse_and_validate
from app.llm.provider import (
    PROVIDER_DEEPSEEK,
    DeepSeekProvider,
    LLMProvider,
    LLMUnavailableError,
)
from app.llm.schemas import LLMRequest, LLMResult

BASE_URL = "https://deepseek.test"
EXPECTED_URL = f"{BASE_URL}/chat/completions"

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]


class _Finding(BaseModel):
    risk_title: str
    level: str
    quote: str


def _settings(*, api_key: str = "sk-test", model: str = "deepseek-chat") -> AgentSettings:
    """构造配置实例 —— 不读 .env（``_env_file=None``），因此与开发机配置无关。"""
    return AgentSettings(
        _env_file=None,
        deepseek_api_key=api_key,
        deepseek_base_url=BASE_URL,
        deepseek_model=model,
    )


def _request(**overrides: Any) -> LLMRequest:
    payload: dict[str, Any] = {
        "scene": LLMScene.CLAUSE_REVIEW,
        "system_prompt": "你是合同审查助手。",
        "user_prompt": "请审查以下条款：……",
        "output_schema": _Finding,
        "prompt_version": "clause_review.v1",
    }
    payload.update(overrides)
    return LLMRequest(**payload)


def _completion(content: str, **extra: Any) -> dict[str, Any]:
    body = {
        "id": "req-123",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 45},
    }
    body.update(extra)
    return body


@pytest.fixture
async def make_provider() -> AsyncIterator[Callable[..., DeepSeekProvider]]:
    """构造一个挂了 MockTransport 的 Provider，并在用例结束时关闭底层 client。"""
    opened: list[httpx.AsyncClient] = []

    def _make(handler: Handler, settings: AgentSettings | None = None) -> DeepSeekProvider:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opened.append(client)
        return DeepSeekProvider(settings or _settings(), client=client)

    yield _make

    for client in opened:
        await client.aclose()


# --------------------------------------------------------------------------- #
# 未配置：本地短路，不发请求
# --------------------------------------------------------------------------- #
async def test_missing_api_key_short_circuits_without_any_request(make_provider) -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(request)
        return httpx.Response(200, json=_completion("{}"))

    provider = make_provider(handler, settings=_settings(api_key=""))

    with pytest.raises(LLMUnavailableError) as excinfo:
        await provider.complete(_request())

    assert calls == [], "未配置就**不许**发请求 —— 拿空 key 去换 401 只是把本地事实伪装成远端错误"
    assert excinfo.value.status_code is None
    assert "未配置" in str(excinfo.value)


async def test_missing_model_also_short_circuits(make_provider) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("不该被调用")

    provider = make_provider(handler, settings=_settings(model=""))

    with pytest.raises(LLMUnavailableError):
        await provider.complete(_request())


# --------------------------------------------------------------------------- #
# 正常调用：请求形状
# --------------------------------------------------------------------------- #
async def test_posts_to_the_chat_completions_endpoint(make_provider) -> None:
    captured: list[httpx.Request] = []
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        bodies.append(json.loads(await request.aread()))
        return httpx.Response(200, json=_completion('{"risk_title": "x", "level": "HIGH", "quote": "y"}'))

    provider = make_provider(handler)

    await provider.complete(_request())

    assert str(captured[0].url) == EXPECTED_URL
    assert captured[0].method == "POST"
    assert captured[0].headers["authorization"] == "Bearer sk-test"
    assert bodies[0]["model"] == "deepseek-chat"
    assert bodies[0]["temperature"] == 0.2
    assert bodies[0]["max_tokens"] == 4096
    assert [m["role"] for m in bodies[0]["messages"]] == ["system", "user"]


async def test_structured_request_carries_protocol_level_json_constraint(make_provider) -> None:
    """第 1 道防线：要求结构化输出时，请求体必须带 ``response_format``。"""
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx.Response(200, json=_completion("{}"))

    await make_provider(handler).complete(_request())

    assert bodies[0]["response_format"] == {"type": "json_object"}


async def test_structured_request_injects_the_generated_schema(make_provider) -> None:
    """第 2 道防线：schema 由 Pydantic 生成并注入 system prompt。"""
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx.Response(200, json=_completion("{}"))

    await make_provider(handler).complete(_request())

    system_content = bodies[0]["messages"][0]["content"]
    assert system_content.startswith("你是合同审查助手。")
    assert '"risk_title"' in system_content


async def test_plain_request_carries_no_json_constraint(make_provider) -> None:
    """不需要 JSON 的场景不能被硬掰成 JSON。"""
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx.Response(200, json=_completion("随便一段话"))

    await make_provider(handler).complete(_request(output_schema=None))

    assert "response_format" not in bodies[0]
    assert bodies[0]["messages"][0]["content"] == "你是合同审查助手。"


# --------------------------------------------------------------------------- #
# 正常调用：结果封装
# --------------------------------------------------------------------------- #
async def test_wraps_the_response_into_a_result(make_provider) -> None:
    raw = '{"risk_title": "无限责任", "level": "HIGH", "quote": "全部损失"}'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(raw))

    result = await make_provider(handler).complete(_request())

    assert result.raw_text == raw
    assert result.model == "deepseek-chat"
    assert result.provider == PROVIDER_DEEPSEEK
    assert (result.prompt_tokens, result.completion_tokens) == (120, 45)
    assert result.request_id == "req-123"
    assert result.attempts == 1
    assert result.latency_ms >= 0


async def test_provider_does_not_parse_business_objects(make_provider) -> None:
    """Provider **不解析**：``parsed`` 恒为空，回填是调用方的事。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion('{"risk_title": "x", "level": "HIGH", "quote": "y"}'))

    result = await make_provider(handler).complete(_request())

    assert result.parsed is None


async def test_non_json_content_is_wrapped_verbatim(make_provider) -> None:
    """模型回了一段人话 —— Provider 照收不误，**判断不是它的事**。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("抱歉，我无法回答这个问题。"))

    result = await make_provider(handler).complete(_request())

    assert result.raw_text == "抱歉，我无法回答这个问题。"
    # 内容不合契约由 json_guard 判定 —— 两步的失败是分开的
    with pytest.raises(LLMSchemaInvalidError):
        parse_and_validate(_Finding, result)


async def test_end_to_end_provider_then_guard(make_provider) -> None:
    """Provider 收 → guard 校验，两步拼起来才是"拿到一个可用的结构化输出"。"""
    raw = '{"risk_title": "无限责任", "level": "HIGH", "quote": "全部损失"}'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(raw))

    result = await make_provider(handler).complete(_request())
    validated = parse_and_validate(_Finding, result)

    assert isinstance(validated.parsed, _Finding)
    assert validated.parsed.quote == "全部损失"


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status_code", [400, 401, 429, 500, 503])
async def test_http_error_raises_unavailable(make_provider, status_code: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "boom"}})

    with pytest.raises(LLMUnavailableError) as excinfo:
        await make_provider(handler).complete(_request())

    assert excinfo.value.status_code == status_code
    assert excinfo.value.error_code == AgentErrorCode.LLM_UNAVAILABLE.value
    assert str(status_code) in str(excinfo.value)


async def test_transport_failure_raises_unavailable(make_provider) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMUnavailableError) as excinfo:
        await make_provider(handler).complete(_request())

    assert excinfo.value.status_code is None


async def test_timeout_raises_unavailable(make_provider) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(LLMUnavailableError):
        await make_provider(handler).complete(_request())


async def test_non_json_body_raises_unavailable(make_provider) -> None:
    """2xx 但响应体不是 JSON —— 属于"没能问出结果"，不是"内容不合契约"。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    with pytest.raises(LLMUnavailableError) as excinfo:
        await make_provider(handler).complete(_request())

    assert excinfo.value.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},  # 缺 choices
        {"choices": []},  # 空 choices
        {"choices": [{}]},  # 缺 message
        {"choices": [{"message": {}}]},  # 缺 content
        {"choices": [{"message": {"content": None}}]},  # content 不是字符串
    ],
)
async def test_malformed_success_body_raises_unavailable(make_provider, body: dict[str, Any]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(LLMUnavailableError):
        await make_provider(handler).complete(_request())


async def test_error_message_is_bounded(make_provider) -> None:
    """报错信息不能把整个响应体搬进来（可能很长）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="x" * 5000)

    with pytest.raises(LLMUnavailableError) as excinfo:
        await make_provider(handler).complete(_request())

    assert len(str(excinfo.value)) < 1000


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
async def test_injected_client_is_not_closed_by_the_provider(make_provider) -> None:
    """注入的 client 归调用方管 —— 与 ``BackendClient`` 同一约定。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("{}"))

    provider = make_provider(handler)
    await provider.complete(_request())

    await provider.aclose()

    assert provider._client is not None, "注入的 client 不该被 Provider 关掉"


async def test_aclose_is_idempotent() -> None:
    """重复关闭不报错 —— 应用关闭路径可能被走两次（异常退出、测试 teardown）。"""
    provider = DeepSeekProvider(_settings())

    await provider.aclose()
    await provider.aclose()


# --------------------------------------------------------------------------- #
# 应用级生命周期契约（P9-6a 修正）
#
# 应用不仅在业务里调 ``complete()``，还在关闭时 ``await provider.aclose()``
# （见 app/main.py 的 lifespan）—— 因此它必须是 **Protocol 的一部分**。
# --------------------------------------------------------------------------- #
def test_protocol_declares_the_whole_lifecycle() -> None:
    """契约包含 **两个** 方法，缺一不可：``complete()`` 与 ``aclose()``。"""
    methods = {
        name
        for name, member in inspect.getmembers(LLMProvider, inspect.isfunction)
        if not name.startswith("_")  # 忽略 Protocol 自带的 __init__ 等
    }

    assert methods == {"complete", "aclose"}


def test_the_real_provider_satisfies_the_protocol() -> None:
    assert isinstance(DeepSeekProvider(_settings()), LLMProvider)


def test_a_double_missing_aclose_does_not_satisfy_the_protocol() -> None:
    """这条断言让上面那个检查**有牙齿**：漏掉 ``aclose`` 的替身会被判不合契约。

    真实代价不是"类型不对"，而是**关闭阶段才炸** ——
    一个看起来像测试基础设施故障、实则契约缺失的错误。
    """

    class _HalfProvider:
        async def complete(self, request: LLMRequest) -> LLMResult:  # pragma: no cover - 不会被调用
            raise NotImplementedError

    assert not isinstance(_HalfProvider(), LLMProvider)
    assert not isinstance(object(), LLMProvider)
