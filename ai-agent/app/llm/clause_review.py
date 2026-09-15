"""条款审查调用闭环（P9-5 最小子步骤）：把一次 clause_review 从头串到尾。

三层边界（本模块是**最上面那层**）
--------------------------------
::

    业务层（本模块）      ClauseReviewPromptInput → LLMRequest
                          （选哪个版本的提示 + 渲染 user prompt + 声明输出 schema）
        │
        ▼
    Provider（protocol）  HTTP 调用 → LLMResult（**原始**，不解析、不判断）
        │
        ▼
    json_guard            机械 JSON 约束 + 严格 Pydantic 校验 → LLMReviewResult
        │
        ▼
    ClauseReviewOutcome   结构化结论 + 原始调用留痕

**本模块只做编排**：不认识 HTTP（Provider 的）、不认识 JSON（json_guard 的）、
不认识文档结构（定位是 P9-3 的事）。它负责的是**业务决定**：

* 用哪个场景（``LLMScene.CLAUSE_REVIEW``）与哪个提示版本
* 提示正文从哪里读（``prompts/clause_review.v1.md``）
* 输出契约声明成什么（``LLMReviewResult``）—— 它同时决定注入提示的 schema
* 把三层的产物拼成一个可消费的结果

失败怎么处理（刻意**不接**）
--------------------------
Provider 的 :class:`~app.llm.provider.LLMUnavailableError` 与 json_guard 的
:class:`~app.llm.json_guard.LLMSchemaInvalidError` **原样向上冒**：
§9.1 第 4 道防线规定"本批降级为仅规则引擎结果并标记 warning"——
**降级是调用方（将来的 ``llm_review`` 节点）的决定**，不是这一层的。
在这一层吞掉异常，等于把"这批没跑成"悄悄变成一个空结论。

本步**不做**：Graph / State / Response DTO / Merge / scorer / 统一风险模型 /
重试 / 队列 / 真实网络依赖（测试全部用 fake provider）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

from app.core.constants import LLMScene
from app.llm.findings import ClauseReviewPromptInput, LLMReviewResult
from app.llm.json_guard import parse_and_validate
from app.llm.prompts import PROMPT_CLAUSE_REVIEW_V1, load_prompt, render_clause_review_user_prompt
from app.llm.provider import LLMProvider
from app.llm.schemas import LLMRequest, LLMResult

logger = logging.getLogger(__name__)

#: 条款审查的输出契约（**结构定义的单一事实来源**）——
#: 它既用于校验模型输出，也用于生成注入提示的 JSON Schema（见 json_guard）。
CLAUSE_REVIEW_SCHEMA = LLMReviewResult


@dataclass(frozen=True, slots=True)
class ClauseReviewOutcome:
    """一次条款审查调用的结果。

    ``llm`` 是**原始调用留痕**（``raw_text`` / ``model`` / token 数 / 耗时）：
    将来写 ``ai_call_log`` 要的就是它（§9.1）。结构化结论从 :attr:`review` 取。
    """

    llm: LLMResult

    @property
    def review(self) -> LLMReviewResult:
        """结构化结论。

        为什么是 property 而不是字段：``llm.parsed`` **已经是它了**，
        再存一个字段就是同一个结论的两份真相源。
        ``parse_and_validate`` 要么抛出、要么把 ``parsed`` 填上，
        因此能走到这里时它一定是一个 :class:`LLMReviewResult`。
        """
        return cast(LLMReviewResult, self.llm.parsed)


def build_clause_review_request(
    payload: ClauseReviewPromptInput,
    *,
    prompt_version: str = PROMPT_CLAUSE_REVIEW_V1,
) -> LLMRequest:
    """把一次审查的输入组织成 :class:`LLMRequest`（**不发起任何调用**）。

    单独提出来是为了让"组织请求"这一步可以脱离网络被检查 ——
    测"这条请求该长什么样"不需要一个 provider。

    ⚠️ ``system_prompt`` 只放**业务口径**（``prompts/*.md`` 的正文）；
    结构说明由 Provider 在 ``output_schema`` 非空时用**同一个 Pydantic 模型**
    生成并追加 —— 这里**不**再抄一份 JSON Schema。

    :raises FileNotFoundError: 提示版本不存在（部署错误，必须响亮失败）
    """
    return LLMRequest(
        scene=LLMScene.CLAUSE_REVIEW,
        system_prompt=load_prompt(prompt_version),
        user_prompt=render_clause_review_user_prompt(payload),
        output_schema=CLAUSE_REVIEW_SCHEMA,
        prompt_version=prompt_version,
    )


async def review_clauses(
    provider: LLMProvider,
    payload: ClauseReviewPromptInput,
    *,
    prompt_version: str = PROMPT_CLAUSE_REVIEW_V1,
) -> ClauseReviewOutcome:
    """跑一次"条款审查"调用：组织请求 → 调用模型 → 校验结构化输出。

    :param provider: :class:`~app.llm.provider.LLMProvider` —— 真实实现或测试用的 fake
    :param payload: 本批条款与规则已命中的提示
    :param prompt_version: 提示版本（**同时是 ``prompts/`` 下的文件名**）
    :return: 结构化结论 + 原始调用留痕
    :raises LLMUnavailableError: 模型没能给出可用回答（由 Provider 抛出，**不接**）
    :raises LLMSchemaInvalidError: 回答不是合法 JSON / 不满足输出契约（**不接**）
    """
    request = build_clause_review_request(payload, prompt_version=prompt_version)
    result = await provider.complete(request)
    validated = parse_and_validate(CLAUSE_REVIEW_SCHEMA, result)
    review = cast(LLMReviewResult, validated.parsed)

    logger.info(
        "clause_review 完成 | prompt_version=%s clauses=%d findings=%d model=%s "
        "prompt_tokens=%d completion_tokens=%d latency_ms=%d",
        prompt_version,
        len(payload.clauses),
        len(review.findings),
        validated.model,
        validated.prompt_tokens,
        validated.completion_tokens,
        validated.latency_ms,
    )
    return ClauseReviewOutcome(llm=validated)


__all__ = ["CLAUSE_REVIEW_SCHEMA", "ClauseReviewOutcome", "build_clause_review_request", "review_clauses"]
