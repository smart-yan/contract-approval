"""LLM 能力域（P9）。

把「调用一次大模型并拿到结构化输出」这件事的**基础设施**收在一处：

* ``schemas``   —— ``LLMRequest`` / ``LLMResult``（只有调用所需字段，**无业务字段**）
* ``provider``  —— ``LLMProvider`` 协议 + ``DeepSeekProvider``（只管传输）
* ``json_guard``—— 结构化输出的基础防线（schema 生成 / 注入 / 解析 / 严格校验）

分工的原则：**传输失败与输出不合契约是两件事**，
分别由 :class:`~app.llm.provider.LLMUnavailableError` 与
:class:`~app.llm.json_guard.LLMSchemaInvalidError` 表达，互不掩盖。

⚠️ P9-1 只到基础设施为止。**业务审查**（``llm_review`` 节点、prompt 文件、
findings 契约、定位、合并、scoring）**尚未实现** ——
本包目前不认识 Clause、Risk、合同，也不做任何判断。
"""

from app.llm.json_guard import (
    LLMSchemaInvalidError,
    build_schema_instruction,
    extract_json_object,
    json_schema_for,
    parse_and_validate,
)
from app.llm.provider import PROVIDER_DEEPSEEK, DeepSeekProvider, LLMProvider, LLMUnavailableError
from app.llm.schemas import LLMRequest, LLMResult

__all__ = [
    "PROVIDER_DEEPSEEK",
    "DeepSeekProvider",
    "LLMProvider",
    "LLMRequest",
    "LLMResult",
    "LLMSchemaInvalidError",
    "LLMUnavailableError",
    "build_schema_instruction",
    "extract_json_object",
    "json_schema_for",
    "parse_and_validate",
]
