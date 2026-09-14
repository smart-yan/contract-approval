"""LLM 结构化输出的**基础防线**（P9-1）。

对应架构文档 §9.1「统一结构化输出保障链」的四道防线，本模块负责 **1 / 2 / 4**：

========  ==========================================================  ==========
防线       做什么                                                      在哪
========  ==========================================================  ==========
1 协议层   请求侧带 JSON 输出约束（``response_format``）                 provider
2 提示层   把 **Pydantic 生成的** JSON Schema 注入 prompt                **本模块**
3 解析层   剥围栏 → ``json.loads`` →（失败则修复重试）                  **本模块（不含重试）**
4 校验层   Pydantic 严格校验，失败 → ``LLM_SCHEMA_INVALID``             **本模块**
========  ==========================================================  ==========

**第 3 道防线里的"修复重试"属于扩展阶段，P9 明确不做**（§9.1 的裁决：
"P9 阶段 JSON 解析失败即视为该批次失败并如实记录，不做自动修复 —— 先跑通，再加固"）。
因此本模块只做"剥围栏 + 解析"，**一失败就抛**，不重试、不纠错、不猜。

单一事实来源：schema 只有一份
--------------------------
结构定义**只在 Pydantic 模型里写一次**（:func:`json_schema_for` 直接取
``model_json_schema()``），prompt 里注入的那段由**同一个**模型生成。
手抄第二份 JSON Schema 是这类系统最经典的漂移源：模型改了、抄本没改，
于是 prompt 描述的字段与真正校验的字段悄悄不一致，而且**不会有任何测试报错**。

与"业务"的分界
------------
本模块只注入**机械约束**（只输出一个 JSON 对象、不要包代码块、结构如下）。
§9.1 里那三条硬性规则中的"不得编造条款""quote 必须逐字复制原文"**属于业务口径**，
写在 P9-2 的 prompt 文件里，不在这一层。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.errors import AgentErrorCode
from app.llm.schemas import LLMResult

#: 模型有时会把 JSON 包在 ```json 围栏里。协议层的 ``response_format`` 本该避免这种情况，
#: 但"本该"不是"一定"，剥一次围栏的成本远低于丢掉一整批审查结果。
_FENCE = "```"


class LLMSchemaInvalidError(Exception):
    """LLM 的输出**不合契约**：不是合法 JSON，或过不了请求里声明的 schema。

    与 :class:`~app.llm.provider.LLMUnavailableError` 的区别是**责任方**：
    那个是"模型/网络没给出回答"（我们可以重试或降级），
    这个是"模型回答了我们，但回答的内容不能用"（要么改 prompt，要么丢这一条）。
    把两者混成一个异常，排查时就分不清"该重试"还是"该改 prompt"。

    ⚠️ 它**不会**让整个任务失败：§9.1 第 4 道防线规定"仍失败则本批降级为
    **仅规则引擎结果**并在任务上标记 warning"。接住它并降级是 P9-2 节点的事。
    """

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code or AgentErrorCode.LLM_SCHEMA_INVALID.value


# --------------------------------------------------------------------------- #
# 第 2 道防线：把 schema 注入 prompt
# --------------------------------------------------------------------------- #
def json_schema_for(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic 模型 → JSON Schema（**唯一的结构定义来源**）。"""
    return schema.model_json_schema()


def build_schema_instruction(schema: type[BaseModel]) -> str:
    """生成注入 prompt 的那段说明 —— 只含机械约束，不含业务口径。"""
    return (
        "输出必须是**一个 JSON 对象**，不要输出任何解释文字，不要用 Markdown 代码块包裹。\n"
        "对象必须满足下面的 JSON Schema：\n"
        f"{json.dumps(json_schema_for(schema), ensure_ascii=False, indent=2)}"
    )


# --------------------------------------------------------------------------- #
# 第 3 道防线（不含重试）：原始文本 → JSON 对象
# --------------------------------------------------------------------------- #
def extract_json_object(raw_text: str) -> dict[str, Any]:
    """把模型返回的原始文本解析成 JSON **对象**。

    只做两件事：剥掉一层 ``` 围栏、``json.loads``。**不修复、不重试、不猜**。

    :raises LLMSchemaInvalidError: 空文本 / 非法 JSON / 合法 JSON 但不是对象
    """
    text = raw_text.strip()
    if not text:
        raise LLMSchemaInvalidError("LLM 返回了空文本，无法解析为 JSON")

    if text.startswith(_FENCE):
        # 去掉首行的 ```json / ``` 与结尾的 ```；没有结尾围栏也照样剥（截断的输出很常见）
        lines = text.splitlines()
        text = "\n".join(lines[1:]).rstrip()
        if text.endswith(_FENCE):
            text = text[: -len(_FENCE)].rstrip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMSchemaInvalidError(f"LLM 的输出不是合法 JSON：{exc}") from exc

    if not isinstance(payload, dict):
        raise LLMSchemaInvalidError(f"LLM 的输出必须是 JSON 对象，实际是 {type(payload).__name__}")
    return payload


# --------------------------------------------------------------------------- #
# 第 4 道防线：Pydantic 严格校验
# --------------------------------------------------------------------------- #
def parse_and_validate(schema: type[BaseModel], result: LLMResult) -> LLMResult:
    """校验 ``result.raw_text`` 并**回填** ``result.parsed``（返回副本，不改原对象）。

    用 ``strict=True``：LLM 把数字写成字符串（``"3"``）这类"看起来能自动修好"的情况
    **一律判失败**。理由是可观测性 —— 静默的类型强转会掩盖 prompt 与 schema 的口径分歧，
    而那种分歧会随着模型版本变化突然发作。宁可让这一批降级并留下记录。

    :raises LLMSchemaInvalidError: 解析失败或校验失败
    """
    payload = extract_json_object(result.raw_text)
    try:
        parsed = schema.model_validate(payload, strict=True)
    except ValidationError as exc:
        raise LLMSchemaInvalidError(
            f"LLM 的输出不满足 {schema.__name__} 的契约：{exc.error_count()} 处错误 —— {exc}"
        ) from exc
    return result.model_copy(update={"parsed": parsed})


__all__ = [
    "LLMSchemaInvalidError",
    "build_schema_instruction",
    "extract_json_object",
    "json_schema_for",
    "parse_and_validate",
]
