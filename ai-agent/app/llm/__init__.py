"""LLM 能力域（P9）。

把「调用一次大模型并拿到结构化输出」这件事的**基础设施**收在一处：

* ``schemas``   —— ``LLMRequest`` / ``LLMResult``（只有调用所需字段，**无业务字段**）
* ``provider``  —— ``LLMProvider`` 协议 + ``DeepSeekProvider``（只管传输）
* ``json_guard``—— 结构化输出的基础防线（schema 生成 / 注入 / 解析 / 严格校验）
* ``findings``  —— ``clause_review`` 场景的**领域契约**（``LLMFinding`` / 提示输入）
* ``prompts``   —— 版本化提示文本与渲染（``prompts/*.md``，版本即文件名）
* ``finding_resolution`` —— findings 落地解析：定位 + ``related_rule_code`` 核对（P9-4）

分工的原则：**传输失败与输出不合契约是两件事**，
分别由 :class:`~app.llm.provider.LLMUnavailableError` 与
:class:`~app.llm.json_guard.LLMSchemaInvalidError` 表达，互不掩盖。

⚠️ 进度：契约（P9-2）、提示（P9-2）、定位（P9-3）、落地解析（P9-4）已就绪。
``llm_review`` 节点、真实调用流程、合并、scoring **仍未实现** ——
本包目前没有任何地方会真的发一次 LLM 请求。
"""

from app.llm.finding_resolution import ResolvedFinding, resolve_findings
from app.llm.findings import (
    ClauseContext,
    ClauseReviewPromptInput,
    LLMFinding,
    LLMReviewResult,
    MatchedRuleHint,
    Suggestion,
)
from app.llm.json_guard import (
    LLMSchemaInvalidError,
    build_schema_instruction,
    extract_json_object,
    json_schema_for,
    parse_and_validate,
)
from app.llm.prompts import PROMPT_CLAUSE_REVIEW_V1, load_prompt, render_clause_review_user_prompt
from app.llm.provider import PROVIDER_DEEPSEEK, DeepSeekProvider, LLMProvider, LLMUnavailableError
from app.llm.schemas import LLMRequest, LLMResult

__all__ = [
    "PROMPT_CLAUSE_REVIEW_V1",
    "PROVIDER_DEEPSEEK",
    "ClauseContext",
    "ClauseReviewPromptInput",
    "DeepSeekProvider",
    "LLMFinding",
    "LLMProvider",
    "LLMRequest",
    "LLMResult",
    "LLMReviewResult",
    "LLMSchemaInvalidError",
    "LLMUnavailableError",
    "MatchedRuleHint",
    "ResolvedFinding",
    "Suggestion",
    "build_schema_instruction",
    "extract_json_object",
    "json_schema_for",
    "load_prompt",
    "parse_and_validate",
    "render_clause_review_user_prompt",
    "resolve_findings",
]
