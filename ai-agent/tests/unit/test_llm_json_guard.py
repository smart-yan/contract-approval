"""``json_guard`` —— LLM 结构化输出的基础防线（§9.1 的第 2/3/4 道）。

这里用**测试自己的**小 schema（不是任何业务模型）：这一层是通用的，
一旦它需要认识 Clause / Risk 才能工作，就说明边界画错了。
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

from app.core.errors import AgentErrorCode
from app.llm.json_guard import (
    LLMSchemaInvalidError,
    build_schema_instruction,
    extract_json_object,
    json_schema_for,
    parse_and_validate,
)
from app.llm.schemas import LLMResult


class _Finding(BaseModel):
    """一个最小的结构化输出形状（只为测试而存在，不是业务契约）。"""

    risk_title: str
    level: Literal["HIGH", "MEDIUM", "LOW"]
    quote: str


def _result(raw_text: str) -> LLMResult:
    return LLMResult(raw_text=raw_text, model="deepseek-chat", provider="deepseek")


# --------------------------------------------------------------------------- #
# 第 2 道防线：schema 生成与注入
# --------------------------------------------------------------------------- #
def test_json_schema_comes_from_the_pydantic_model() -> None:
    """**单一事实来源**：schema 由模型生成，不是手抄的第二份。"""
    schema = json_schema_for(_Finding)

    assert schema["title"] == "_Finding"
    assert set(schema["properties"]) == {"risk_title", "level", "quote"}
    assert schema["required"] == ["risk_title", "level", "quote"]


def test_schema_instruction_embeds_the_generated_schema() -> None:
    instruction = build_schema_instruction(_Finding)

    assert "JSON" in instruction
    assert '"risk_title"' in instruction
    assert '"HIGH"' in instruction, "枚举取值必须出现在注入的 schema 里"


def test_schema_instruction_does_not_carry_business_rules() -> None:
    """ "不得编造条款""quote 必须逐字复制"属于业务口径，写在 P9-2 的 prompt 里，不在这层。"""
    instruction = build_schema_instruction(_Finding)

    assert "逐字" not in instruction
    assert "编造" not in instruction


def test_schema_instruction_follows_the_model_when_it_changes() -> None:
    """模型加字段 → 注入的 schema 跟着变。手抄的话这里就会漂移。"""

    class _Extended(_Finding):
        related_rule_code: str | None = None

    assert "related_rule_code" not in build_schema_instruction(_Finding)
    assert "related_rule_code" in build_schema_instruction(_Extended)


# --------------------------------------------------------------------------- #
# 第 3 道防线：解析（**不含修复重试**）
# --------------------------------------------------------------------------- #
def test_parses_a_bare_json_object() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_strips_a_markdown_fence() -> None:
    raw = '```json\n{"a": 1}\n```'

    assert extract_json_object(raw) == {"a": 1}


def test_strips_an_unterminated_fence() -> None:
    """输出被截断时结尾没有围栏 —— 照样要能解析。"""
    raw = '```json\n{"a": 1}'

    assert extract_json_object(raw) == {"a": 1}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "这不是 JSON",
        "{不是合法 JSON}",
        "[1, 2, 3]",  # 合法 JSON，但不是对象
        '"just a string"',
    ],
)
def test_invalid_json_raises_schema_invalid(raw: str) -> None:
    with pytest.raises(LLMSchemaInvalidError) as excinfo:
        extract_json_object(raw)

    assert excinfo.value.error_code == AgentErrorCode.LLM_SCHEMA_INVALID.value


def test_does_not_repair_a_broken_payload() -> None:
    """刻意不做修复：少一个引号的 JSON 必须**原样失败**，不许被猜回来。"""
    with pytest.raises(LLMSchemaInvalidError):
        extract_json_object('{"risk_title": "缺引号, "level": "HIGH"}')


# --------------------------------------------------------------------------- #
# 第 4 道防线：严格校验
# --------------------------------------------------------------------------- #
def test_valid_payload_fills_parsed() -> None:
    result = _result('{"risk_title": "无限责任", "level": "HIGH", "quote": "全部损失"}')

    validated = parse_and_validate(_Finding, result)

    assert isinstance(validated.parsed, _Finding)
    assert validated.parsed.level == "HIGH"
    assert validated.raw_text == result.raw_text, "原始文本必须保留（留痕）"


def test_validation_returns_a_copy_not_the_original() -> None:
    result = _result('{"risk_title": "X", "level": "LOW", "quote": "Y"}')

    validated = parse_and_validate(_Finding, result)

    assert result.parsed is None, "原对象不被就地修改"
    assert validated is not result


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"level": "HIGH", "quote": "x"}', "缺 risk_title"),
        ('{"risk_title": "x", "quote": "y"}', "缺 level"),
        ('{"risk_title": "x", "level": "CRITICAL", "quote": "y"}', "level 越界"),
        ('{"risk_title": 1, "level": "HIGH", "quote": "y"}', "类型不对（strict）"),
        ("[]", "不是对象"),
        ("not json at all", "不是 JSON"),
    ],
)
def test_schema_violations_are_reported(raw: str, reason: str) -> None:
    with pytest.raises(LLMSchemaInvalidError):
        parse_and_validate(_Finding, _result(raw))


def test_strict_mode_rejects_coercible_types() -> None:
    """``strict=True``：``"1"`` 不会被悄悄当成数字 —— 那会掩盖 prompt 与 schema 的口径分歧。"""

    class _Numeric(BaseModel):
        count: int

    with pytest.raises(LLMSchemaInvalidError):
        parse_and_validate(_Numeric, _result('{"count": "1"}'))


def test_error_message_names_the_schema() -> None:
    with pytest.raises(LLMSchemaInvalidError) as excinfo:
        parse_and_validate(_Finding, _result("{}"))

    assert "_Finding" in str(excinfo.value)


def test_provider_and_guard_errors_are_distinguishable() -> None:
    """两类失败的 error_code 不同 —— 排查时才能分清"该重试"还是"该改 prompt"。"""
    from app.llm.provider import LLMUnavailableError

    assert LLMUnavailableError("x").error_code == AgentErrorCode.LLM_UNAVAILABLE.value
    assert LLMSchemaInvalidError("x").error_code == AgentErrorCode.LLM_SCHEMA_INVALID.value
    assert LLMUnavailableError("x").error_code != LLMSchemaInvalidError("x").error_code
