"""LLM 调用的请求 / 结果契约（P9-1）。

职责边界
-------
::

    LLMRequest  ──▶  LLMProvider.complete()  ──▶  LLMResult
    （要问什么）        （怎么问、怎么收）          （模型回了什么，原样）

**这里只有"调用一次模型"所需要的东西，没有任何业务字段** ——
不认识 Clause / Risk / AgentRule，也不认识 prompt 的内容。
业务侧的 findings 契约属于 P9-2（``llm_review`` 节点与它的 prompt 文件）。

``parsed`` 谁来填
---------------
**不是 Provider。** Provider 只负责"模型调用与原始结果封装"（它连 JSON 都不解析），
``parsed`` 由调用方经 ``json_guard.parse_and_validate()`` 校验后回填 ——
这样"传输失败"与"输出不合契约"是两件互不掩盖的事：
前者是 :class:`~app.llm.provider.LLMUnavailableError`，
后者是 :class:`~app.llm.json_guard.LLMSchemaInvalidError`。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import LLMScene


class LLMRequest(BaseModel):
    """一次 LLM 调用。字段口径沿用架构文档 §9.1。"""

    #: 允许 ``output_schema`` 这类"类对象"字段（API key 除外，它不在这里）
    model_config = ConfigDict(arbitrary_types_allowed=True)

    scene: LLMScene = Field(description="调用场景，取值见 core.constants.LLMScene；会写进 ai_call_log")
    system_prompt: str = Field(description="系统提示（**业务内容**，由 P9-2 的 prompt 文件提供）")
    user_prompt: str = Field(description="用户提示（**业务内容**）")
    output_schema: type[BaseModel] | None = Field(
        default=None,
        description="非空 = 要求结构化输出：请求侧会带上 JSON 输出约束，"
        "并把该 schema 注入 prompt；响应侧由 json_guard 用它校验。"
        "**它是结构定义的单一事实来源** —— 不要另写一份手抄的 JSON Schema",
    )
    temperature: float = Field(default=0.2, ge=0.0, le=2.0, description="采样温度")
    max_tokens: int = Field(default=4096, gt=0, description="输出上限")
    timeout: float = Field(default=120.0, gt=0, description="本次调用超时（秒）")
    prompt_version: str = Field(
        min_length=1,
        description="提示版本（如 clause_review.v1）。"
        "⚠️ 它会进入 ``review_task.idempotency_key`` 的计算，"
        "**空字符串必须被拦住** —— 否则「换过 prompt 却不产生新任务」会静默发生",
    )


class LLMResult(BaseModel):
    """一次 LLM 调用的结果 —— **模型回了什么，原样封装**。

    ⚠️ ``raw_text`` 可能**不是**合法 JSON（模型不听话、被截断、返回了一段解释）：
    Provider 不做任何判断，照样原样带回来 —— 判断是 :mod:`app.llm.json_guard` 的事。
    把"拿到响应"与"响应合格"分开，才能在日志里说清楚到底是哪一步出的问题。
    """

    parsed: BaseModel | None = Field(
        default=None,
        description="**已通过 Pydantic 校验**的结构化对象；由调用方经 json_guard 回填，"
        "Provider 恒不填（未启用结构化输出时也恒为 None）",
    )
    raw_text: str = Field(default="", description="模型返回的原始文本，留痕用（不保证是合法 JSON）")
    model: str = Field(description="实际使用的模型名（以便回溯「这条结论是哪版模型给的」）")
    provider: str = Field(description="提供方标识，如 deepseek")
    prompt_tokens: int = Field(default=0, description="输入 token 数")
    completion_tokens: int = Field(default=0, description="输出 token 数")
    latency_ms: int = Field(default=0, description="本次调用耗时（毫秒）")
    request_id: str = Field(default="", description="提供方返回的请求标识，便于对账与排障")
    attempts: int = Field(
        default=1,
        description="实际尝试次数；P9 不做自动修复重试，因此恒为 1（字段留着是因为"
        "§9.1 的第 3 道防线将来会用到，届时不用改契约）",
    )


__all__ = ["LLMRequest", "LLMResult"]
