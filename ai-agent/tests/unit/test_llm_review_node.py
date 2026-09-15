"""``llm_review`` 节点的职责：State → 业务层 → State。

只验证**编解码、输入组装与失败语义**；提示怎么渲染、JSON 怎么校验、
模型怎么调，分别由 P9-2 / P9-1 / P9-1 的测试负责。

**全程离线**：给 ``ReviewContext`` 注入一个 fake provider 就够了 ——
这既是"节点只依赖 protocol"的证据，也让节点测试不依赖网络与真实模型。
"""

from __future__ import annotations

import json
from importlib import import_module
from typing import Any

from langgraph.runtime import Runtime

from app.core.errors import AgentErrorCode
from app.graph.context import ReviewContext
from app.graph.nodes.llm_review import llm_review
from app.graph.state import ContractReviewState
from app.llm.findings import LLMFinding, LLMReviewResult
from app.llm.json_guard import LLMSchemaInvalidError
from app.llm.provider import LLMUnavailableError
from app.llm.schemas import LLMRequest, LLMResult
from app.rules.schemas import RuleRisk
from app.schemas.understanding import Clause
from app.understanding.clauses import identify_clauses
from tests.factories import make_parse_result, para

#: 模块对象（``nodes/__init__.py`` 重导出了同名函数，字符串 monkeypatch 会指到函数上）
_LLM_REVIEW_MODULE = import_module("app.graph.nodes.llm_review")

DOCUMENT = (
    "第一条 知识产权",
    "本项目产生的知识产权归乙方所有。",
    "第二条 违约责任",
    "乙方应承担违约责任。",
)

#: 一份**合法**的模型输出（顶层必须是对象，见 P9-2 的契约）
GOOD_FINDING: dict[str, Any] = {
    "clause_index": 0,
    "risk_title": "知识产权归属相对方",
    "risk_level": "HIGH",
    "reason": "成果归属供方会限制我方后续使用。",
    "quote": "知识产权归乙方所有",
    "context_before": "本项目产生的",
    "context_after": "。",
}


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #
class FakeProvider:
    """只实现 ``LLMProvider`` 协议：记录请求，返回预设结果或抛预设异常。"""

    def __init__(
        self, *, findings: list[dict[str, Any]] | None = None, error: Exception | None = None
    ) -> None:
        self.requests: list[LLMRequest] = []
        self._raw = json.dumps({"findings": findings if findings is not None else []}, ensure_ascii=False)
        self._error = error

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return LLMResult(raw_text=self._raw, model="fake-model", provider="fake", latency_ms=3)


class _FakeBackend:
    """``ReviewContext`` 需要 backend；本节点用不到它，给个占位即可。"""


def _clauses(*texts: str) -> list[Clause]:
    return identify_clauses(make_parse_result(*[para(text) for text in texts]))


def _risk(**overrides: Any) -> RuleRisk:
    payload: dict[str, Any] = {
        "risk_code": "IP_OWNER_SUPPLIER_001",
        "risk_title": "知识产权归属相对方",
        "dimension": "知识产权",
        "risk_level": "HIGH",
        "reason": "命中规则关键词",
        "original_text": "本项目产生的知识产权归乙方所有。",
        "paragraph_index": 1,
        "quote": "知识产权归乙方",
    }
    payload.update(overrides)
    return RuleRisk(**payload)


def _state(**overrides: Any) -> ContractReviewState:
    state: dict[str, Any] = {
        "file_id": 7,
        "contract_type": "PURCHASE",
        "clauses": _clauses(*DOCUMENT),
    }
    state.update(overrides)
    return state


async def _run(state: ContractReviewState, provider: FakeProvider | None) -> dict[str, object]:
    context = ReviewContext(backend=_FakeBackend(), llm=provider)
    return await llm_review(state, Runtime(context=context))


# --------------------------------------------------------------------------- #
# 正常闭环
# --------------------------------------------------------------------------- #
async def test_writes_findings_into_state() -> None:
    provider = FakeProvider(findings=[GOOD_FINDING])

    updates = await _run(_state(), provider)

    assert set(updates) == {"llm_findings"}, "只写这一个键，不污染其它 State 字段"
    (finding,) = updates["llm_findings"]
    assert isinstance(finding, LLMFinding)
    assert finding.risk_title == "知识产权归属相对方"
    assert finding.risk_level == "HIGH"


async def test_node_calls_the_service_exactly_once() -> None:
    provider = FakeProvider(findings=[GOOD_FINDING])

    await _run(_state(), provider)

    assert len(provider.requests) == 1, "一次调用（本步不做分批、不做重试）"


async def test_no_findings_is_a_normal_outcome() -> None:
    """模型没发现问题 → 空列表（**不是错误**，也不编造风险）。"""
    updates = await _run(_state(), FakeProvider(findings=[]))

    assert updates == {"llm_findings": []}


async def test_findings_order_is_preserved() -> None:
    findings = [dict(GOOD_FINDING, risk_title="A"), dict(GOOD_FINDING, risk_title="B")]

    updates = await _run(_state(), FakeProvider(findings=findings))

    assert [f.risk_title for f in updates["llm_findings"]] == ["A", "B"]


# --------------------------------------------------------------------------- #
# 输入组装：State 里已有的东西原样进提示
# --------------------------------------------------------------------------- #
async def test_payload_carries_the_document_clauses() -> None:
    provider = FakeProvider()

    await _run(_state(), provider)

    user_prompt = provider.requests[0].user_prompt
    assert "PURCHASE" in user_prompt
    assert "条款 #0" in user_prompt
    assert "本项目产生的知识产权归乙方所有。" in user_prompt, "条款全文必须原样出现"


async def test_payload_maps_rule_risks_into_rule_hints() -> None:
    """规则命中转成"规则已命中"提示，**rule_code 必须在里面**（否则模型只能编关联码）。"""
    provider = FakeProvider()

    await _run(_state(rule_risks=[_risk()]), provider)

    user_prompt = provider.requests[0].user_prompt
    assert "IP_OWNER_SUPPLIER_001" in user_prompt
    assert "知识产权归属相对方" in user_prompt
    assert "知识产权归乙方" in user_prompt


async def test_no_rule_risks_still_renders() -> None:
    provider = FakeProvider()

    await _run(_state(rule_risks=[]), provider)

    assert "（无）" in provider.requests[0].user_prompt


async def test_request_uses_the_versioned_prompt_and_schema() -> None:
    """节点**不**自己拼提示、不自己拼 schema —— 用的是业务层那一套。"""
    provider = FakeProvider()

    await _run(_state(), provider)

    request = provider.requests[0]
    assert request.prompt_version == "clause_review.v1"
    assert request.output_schema is LLMReviewResult
    assert "不得编造条款" in request.system_prompt, "业务口径的提示来自版本化文件"


async def test_node_does_not_touch_the_input_state() -> None:
    state = _state(rule_risks=[_risk()])
    before = {key: value for key, value in state.items()}

    await _run(state, FakeProvider(findings=[GOOD_FINDING]))

    assert state == before


# --------------------------------------------------------------------------- #
# 失败语义：记录成明确状态，既不吞也不炸
# --------------------------------------------------------------------------- #
async def test_missing_provider_is_recorded_not_crashed() -> None:
    updates = await _run(_state(), None)

    assert updates["error_code"] == AgentErrorCode.LLM_UNAVAILABLE.value
    assert "provider" in updates["error_message"]
    assert "llm_findings" not in updates


async def test_missing_contract_type_is_recorded() -> None:
    state = _state()
    del state["contract_type"]

    updates = await _run(state, FakeProvider())

    assert updates["error_code"] == AgentErrorCode.AGENT_INPUT_INVALID.value
    assert "contract_type" in updates["error_message"]


async def test_model_unavailable_is_recorded_into_state() -> None:
    """模型不可用 → State 里的错误状态（**不冒泡**，也不写一个空结论冒充成功）。"""
    provider = FakeProvider(error=LLMUnavailableError("LLM 返回 503", status_code=503))

    updates = await _run(_state(), provider)

    assert updates["error_code"] == AgentErrorCode.LLM_UNAVAILABLE.value
    assert "503" in updates["error_message"]
    assert "llm_findings" not in updates, "失败时不能留下一个可被读成'没问题'的空列表"


async def test_invalid_schema_is_recorded_into_state() -> None:
    provider = FakeProvider(error=LLMSchemaInvalidError("模型输出不是合法 JSON"))

    updates = await _run(_state(), provider)

    assert updates["error_code"] == AgentErrorCode.LLM_SCHEMA_INVALID.value
    assert "llm_findings" not in updates


async def test_bad_json_from_the_model_never_reaches_state_as_findings() -> None:
    """模型回了一段人话 —— 校验失败进错误状态，**不会**变成"零风险"。"""

    class ChattyProvider(FakeProvider):
        async def complete(self, request: LLMRequest) -> LLMResult:
            self.requests.append(request)
            return LLMResult(raw_text="抱歉，我无法回答。", model="fake", provider="fake")

    updates = await _run(_state(), ChattyProvider())

    assert updates["error_code"] == AgentErrorCode.LLM_SCHEMA_INVALID.value
    assert "llm_findings" not in updates


async def test_empty_document_yields_no_findings_without_calling_the_model() -> None:
    """文档没有可审条款 —— 空结论，且**不为空内容付一次调用**。"""
    provider = FakeProvider()

    updates = await _run(_state(clauses=[]), provider)

    assert updates == {"llm_findings": []}
    assert provider.requests == []


# --------------------------------------------------------------------------- #
# 职责边界：节点不重新实现下游能力
# --------------------------------------------------------------------------- #
async def test_node_delegates_to_the_service(monkeypatch) -> None:
    """节点必须**调用** ``review_clauses``，而不是自己拼提示 / 自己解析 JSON。

    把服务替换成一个哨兵：如果节点里藏着第二套提示或第二份 schema，
    它绕不过这个哨兵，也就拿不到哨兵造出来的那条 finding。
    """
    calls: list[Any] = []
    sentinel = LLMFinding(**GOOD_FINDING)

    class _Outcome:
        review = LLMReviewResult(findings=[sentinel])
        llm = LLMResult(raw_text="{}", model="sentinel", provider="sentinel")

    async def fake_review(provider: Any, payload: Any, **kwargs: Any) -> Any:
        calls.append(payload)
        return _Outcome()

    monkeypatch.setattr(_LLM_REVIEW_MODULE, "review_clauses", fake_review)

    updates = await _run(_state(), FakeProvider())

    assert len(calls) == 1
    assert calls[0].contract_type == "PURCHASE"
    assert [c.clause_index for c in calls[0].clauses] == [0, 1]
    assert updates["llm_findings"] == [sentinel]


def _imported_modules() -> set[str]:
    """节点**代码**里 import 的全部模块名（AST 判定，不看 docstring）。

    ⚠️ 不扫源码文本：本模块 docstring 里正当地写着"不碰 HTTP、不解析 JSON"，
    按文本扫描会把说明文字当成违规。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_LLM_REVIEW_MODULE))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_node_only_depends_on_the_service_and_failure_types() -> None:
    """**依赖集合被钉死**：只有业务层服务、输入契约、以及两个失败类型。

    ``app.llm.json_guard`` / ``app.llm.provider`` 只各取了一个**异常类型**进来
    （把失败翻译成 State 状态要用），没有取它们的任何**能力**
    —— 见下一条对函数名的断言。
    """
    assert _imported_modules() == {
        "__future__",
        "logging",
        "langgraph.runtime",
        "app.core.errors",
        "app.graph.context",
        "app.graph.state",
        "app.llm.clause_review",
        "app.llm.findings",
        "app.llm.json_guard",
        "app.llm.provider",
    }


def test_node_cannot_render_prompts_parse_json_or_send_http() -> None:
    """节点**没有能力**干下面三层的活：没有这些名字，就不可能自己来一套。"""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_LLM_REVIEW_MODULE))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            identifiers.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    for capability in (
        "DeepSeekProvider",  # 自己发 HTTP
        "httpx",
        "load_prompt",  # 自己选/读提示
        "render_clause_review_user_prompt",  # 自己拼 user prompt
        "build_schema_instruction",  # 自己拼 schema
        "parse_and_validate",  # 自己解析 JSON
        "extract_json_object",
    ):
        assert capability not in identifiers, f"节点不该具备 {capability} 的能力"


def test_node_imports_the_service_to_call_it() -> None:
    """正面证据：它**确实**调用了业务层（而不是把逻辑搬进来）。"""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_LLM_REVIEW_MODULE))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "review_clauses" in called


def test_node_is_not_wired_into_the_graph_yet() -> None:
    """本步刻意**不接** builder：接上去会让每一次真实审查都被判成 LLM_UNAVAILABLE。

    （端点尚未注入 provider，"什么时候开始要求 LLM"是接线那一步的裁决。）
    """
    from app.graph import builder

    names = builder.build_review_graph().get_graph().nodes

    assert "llm_review" not in names
