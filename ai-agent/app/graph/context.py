"""Graph 的运行时上下文（LangGraph v1 的 ``context_schema``）。

为什么用 Runtime Context 而不是把依赖塞进 State
---------------------------------------------
httpx client 是**依赖**，不是业务数据。把它放进 State 会立刻违反
"State 只放跨节点流转需要的业务数据"的原则，并且会在后续引入 checkpoint /
持久化时直接变成不可序列化的对象。

LangGraph v1 为此提供了 ``StateGraph(state_schema, context_schema=...)``：

* 节点签名写成 ``(state, runtime)``，依赖从 ``runtime.context`` 取
* 调用时用 ``graph.ainvoke(state, context=ReviewContext(backend=...))`` 注入

好处是测试里换一个 ``BackendClient``（挂 ``httpx.MockTransport``）就能跑，
不需要 monkeypatch 全局单例，也不需要起真实 Backend。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.llm.provider import LLMProvider
from app.tools.backend_client import BackendClient


@dataclass(frozen=True, slots=True)
class ReviewContext:
    """一次 Graph 运行所需的外部依赖。"""

    backend: BackendClient

    #: LLM 提供方（P9-5 引入）。**可空**：不注入时 ``llm_review`` 节点会记录
    #: 明确的 ``LLM_UNAVAILABLE`` 失败，而不是拿一个默认 provider 去悄悄发请求。
    #: 测试注入一个 fake 即可离线跑完整条链路（与 ``backend`` 的注入方式一致）。
    llm: LLMProvider | None = None


__all__ = ["ReviewContext"]
