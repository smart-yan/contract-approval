"""条款审查调用闭环（``llm.clause_review``）。

**不依赖真实网络**：业务层只认识 ``LLMProvider`` 这个 protocol，
测试给它一个 fake 就够 —— 这既是"层边界存在"的证据，也让整套测试离线可跑。

（另有一条用例把**真** Provider + MockTransport 拼起来，验证三层真的能串上。）
"""

from __future__ import annotations

import ast
import inspect
import json
from typing import Any

import httpx
import pytest

from app.core.constants import LLMScene
from app.llm.clause_review import (
    CLAUSE_REVIEW_SCHEMA,
    ClauseReviewOutcome,
    build_clause_review_request,
    review_clauses,
)
from app.llm.findings import (
    ClauseContext,
    ClauseReviewPromptInput,
    LLMFinding,
    LLMReviewResult,
    MatchedRuleHint,
)
from app.llm.json_guard import LLMSchemaInvalidError
from app.llm.prompts import PROMPT_CLAUSE_REVIEW_V1
from app.llm.provider import DeepSeekProvider, LLMUnavailableError
from app.llm.schemas import LLMRequest, LLMResult

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
CLAUSES = [
    ClauseContext(
        clause_index=3,
        clause_type="AMOUNT_PAYMENT",
        clause_no="第三条",
        title="付款方式",
        text="3.1 双方约定的付款计划如下：\n1. 预付款 30% 合同生效后支付",
    )
]

MATCHED = [
    MatchedRuleHint(
        rule_code="PAY_PREPAY_RATIO_001",
        rule_name="预付款比例超过 30%",
        dimension="金额支付",
        risk_level="MEDIUM",
        quote="1. 预付款\t30%",
    )
]

#: 一份**合法**的模型输出（顶层是对象，见 P9-2 的契约）
GOOD_FINDING: dict[str, Any] = {
    "clause_index": 3,
    "risk_title": "付款条款缺少验收前置条件",
    "risk_level": "HIGH",
    "reason": "付款义务先于验收，我方可能在未确认交付质量前即需付款。",
    "quote": "合同生效后支付",
    "context_before": "1. 预付款 30%",
    "context_after": "",
    "related_rule_code": "PAY_PREPAY_RATIO_001",
}


def _payload(**overrides: Any) -> ClauseReviewPromptInput:
    data: dict[str, Any] = {"contract_type": "PURCHASE", "clauses": CLAUSES, "matched_rules": MATCHED}
    data.update(overrides)
    return ClauseReviewPromptInput(**data)


def _llm_result(raw_text: str, **overrides: Any) -> LLMResult:
    data: dict[str, Any] = {
        "raw_text": raw_text,
        "model": "deepseek-chat",
        "provider": "fake",
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "latency_ms": 7,
        "request_id": "req-1",
    }
    data.update(overrides)
    return LLMResult(**data)


class FakeProvider:
    """只实现 ``LLMProvider`` 协议的最小替身：记录请求、返回预设结果或抛预设异常。"""

    def __init__(self, *, raw_text: str = "{}", error: Exception | None = None) -> None:
        self.requests: list[LLMRequest] = []
        self._raw_text = raw_text
        self._error = error

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return _llm_result(self._raw_text)


# --------------------------------------------------------------------------- #
# 请求构造（不发起调用）
# --------------------------------------------------------------------------- #
def test_request_carries_the_scene_and_prompt_version() -> None:
    request = build_clause_review_request(_payload())

    assert request.scene == LLMScene.CLAUSE_REVIEW
    assert request.prompt_version == PROMPT_CLAUSE_REVIEW_V1
    assert request.output_schema is LLMReviewResult


def test_request_system_prompt_is_the_versioned_file() -> None:
    request = build_clause_review_request(_payload())

    assert "不得编造条款" in request.system_prompt
    assert "逐字" in request.system_prompt


def test_request_system_prompt_does_not_hand_copy_a_schema() -> None:
    """结构说明由 Provider 从 Pydantic 模型生成 —— 请求里只放业务口径。"""
    request = build_clause_review_request(_payload())

    assert '"properties"' not in request.system_prompt
    assert '"$defs"' not in request.system_prompt


def test_request_user_prompt_carries_clauses_and_rule_codes() -> None:
    request = build_clause_review_request(_payload())

    assert "条款 #3" in request.user_prompt
    assert CLAUSES[0].text in request.user_prompt
    assert "PAY_PREPAY_RATIO_001" in request.user_prompt, "rule_code 必须出现在输入里"


def test_request_uses_an_explicit_prompt_version() -> None:
    request = build_clause_review_request(_payload(), prompt_version=PROMPT_CLAUSE_REVIEW_V1)

    assert request.prompt_version == "clause_review.v1", "版本会进 ai_call_log 与幂等键"


def test_unknown_prompt_version_fails_loudly_and_never_calls_the_model() -> None:
    """提示版本不存在 = 部署错误 —— 必须响亮失败，且**不该先发一次请求**。"""
    with pytest.raises(FileNotFoundError):
        build_clause_review_request(_payload(), prompt_version="clause_review.v99")


def test_building_a_request_does_not_touch_the_payload() -> None:
    payload = _payload()

    build_clause_review_request(payload)

    assert payload.clauses == CLAUSES and payload.matched_rules == MATCHED


# --------------------------------------------------------------------------- #
# 调用闭环（fake provider）
# --------------------------------------------------------------------------- #
async def test_review_returns_the_structured_result() -> None:
    provider = FakeProvider(raw_text=json.dumps({"findings": [GOOD_FINDING]}, ensure_ascii=False))

    outcome = await review_clauses(provider, _payload())

    assert isinstance(outcome, ClauseReviewOutcome)
    assert isinstance(outcome.review, LLMReviewResult)
    (finding,) = outcome.review.findings
    assert isinstance(finding, LLMFinding)
    assert finding.risk_title == "付款条款缺少验收前置条件"
    assert finding.related_rule_code == "PAY_PREPAY_RATIO_001"


async def test_review_passes_the_built_request_to_the_provider() -> None:
    provider = FakeProvider(raw_text='{"findings": []}')

    await review_clauses(provider, _payload())

    assert len(provider.requests) == 1, "只调用一次（本步不做重试）"
    sent = provider.requests[0]
    assert sent.scene == LLMScene.CLAUSE_REVIEW
    assert sent.prompt_version == PROMPT_CLAUSE_REVIEW_V1
    assert sent.output_schema is LLMReviewResult
    assert "条款 #3" in sent.user_prompt


async def test_review_keeps_the_raw_call_record() -> None:
    """原始留痕（raw_text / model / token / 耗时）必须带出来 —— 将来写 ai_call_log。"""
    raw = json.dumps({"findings": []})
    provider = FakeProvider(raw_text=raw)

    outcome = await review_clauses(provider, _payload())

    assert outcome.llm.raw_text == raw
    assert outcome.llm.model == "deepseek-chat"
    assert (outcome.llm.prompt_tokens, outcome.llm.completion_tokens) == (120, 45)
    assert outcome.llm.latency_ms == 7


async def test_review_with_no_findings_is_a_normal_outcome() -> None:
    """模型没发现问题 → 空结论，**不是错误**（不编造风险）。"""
    provider = FakeProvider(raw_text='{"findings": []}')

    outcome = await review_clauses(provider, _payload())

    assert outcome.review.findings == []


async def test_review_accepts_a_markdown_fenced_answer() -> None:
    """模型偶尔会把 JSON 包在围栏里 —— json_guard 负责剥掉，业务层不用管。"""
    fenced = "```json\n" + json.dumps({"findings": [GOOD_FINDING]}, ensure_ascii=False) + "\n```"
    provider = FakeProvider(raw_text=fenced)

    outcome = await review_clauses(provider, _payload())

    assert len(outcome.review.findings) == 1


# --------------------------------------------------------------------------- #
# 异常边界：本层**不接**
# --------------------------------------------------------------------------- #
async def test_provider_failure_propagates_untouched() -> None:
    """模型没给出可用回答 —— 原样冒泡：降级是调用方的决定，不是这一层的。"""
    provider = FakeProvider(error=LLMUnavailableError("LLM 未配置，未发起任何请求"))

    with pytest.raises(LLMUnavailableError):
        await review_clauses(provider, _payload())


async def test_non_json_answer_raises_schema_invalid() -> None:
    provider = FakeProvider(raw_text="抱歉，我无法回答这个问题。")

    with pytest.raises(LLMSchemaInvalidError):
        await review_clauses(provider, _payload())


async def test_schema_violation_raises_schema_invalid() -> None:
    """等级越界 —— 校验层直接拒，业务层不会拿到半成品。"""
    bad = dict(GOOD_FINDING, risk_level="CRITICAL")
    provider = FakeProvider(raw_text=json.dumps({"findings": [bad]}, ensure_ascii=False))

    with pytest.raises(LLMSchemaInvalidError):
        await review_clauses(provider, _payload())


async def test_bare_array_answer_is_rejected() -> None:
    """顶层必须是对象（``response_format=json_object`` 的硬要求）。"""
    provider = FakeProvider(raw_text=json.dumps([GOOD_FINDING], ensure_ascii=False))

    with pytest.raises(LLMSchemaInvalidError):
        await review_clauses(provider, _payload())


# --------------------------------------------------------------------------- #
# 三层真的能串起来（真 Provider + MockTransport，仍然不碰网络）
# --------------------------------------------------------------------------- #
async def test_works_with_the_real_provider_over_a_stubbed_transport() -> None:
    from app.core.config import AgentSettings

    captured: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(await request.aread()))
        return httpx.Response(
            200,
            json={
                "id": "req-9",
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"findings": [GOOD_FINDING]}, ensure_ascii=False),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    settings = AgentSettings(
        _env_file=None,
        deepseek_api_key="sk-test",
        deepseek_base_url="https://deepseek.test",
        deepseek_model="deepseek-chat",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(settings, client=client)
    try:
        outcome = await review_clauses(provider, _payload())
    finally:
        await client.aclose()

    # 业务层把该带的都带上了：结构化输出约束 + 注入的 schema + 版本化提示
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert '"findings"' in captured[0]["messages"][0]["content"]
    assert "不得编造条款" in captured[0]["messages"][0]["content"]
    assert captured[0]["temperature"] == 0.2

    assert outcome.review.findings[0].risk_title == "付款条款缺少验收前置条件"
    assert outcome.llm.request_id == "req-9"


# --------------------------------------------------------------------------- #
# 契约：本步刻意不做的东西
# --------------------------------------------------------------------------- #
def test_service_keeps_the_provider_and_guard_boundaries() -> None:
    """业务层不碰 HTTP、不碰 JSON 解析细节、不碰文档结构。"""
    import app.llm.clause_review as module

    tree = ast.parse(inspect.getsource(module))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert modules == {
        "__future__",
        "logging",
        "dataclasses",
        "typing",
        "app.core.constants",
        "app.llm.findings",
        "app.llm.json_guard",
        "app.llm.prompts",
        "app.llm.provider",
        "app.llm.schemas",
    }


@pytest.mark.parametrize("forbidden", ["graph", "state", "api", "merge", "scorer", "locator"])
def test_service_does_not_reach_into_later_stages(forbidden: str) -> None:
    """**代码里**不许出现后续阶段的标识符。

    ⚠️ 用 AST 而不是扫源码文本：本模块 docstring 里正当地写着
    "本步不做 Graph / Merge / scorer / 定位"，按文本扫描会把说明文字当成违规。
    """
    import app.llm.clause_review as module

    tree = ast.parse(inspect.getsource(module))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            identifiers.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            identifiers.add(node.module or "")
            identifiers.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    assert not any(forbidden in name.lower() for name in identifiers)


def test_scene_is_hardcoded_to_clause_review() -> None:
    """这个模块**就是**条款审查场景 —— 场景不该由调用方随口传。"""
    parameters = set(inspect.signature(review_clauses).parameters)

    assert "scene" not in parameters
    assert CLAUSE_REVIEW_SCHEMA is LLMReviewResult
