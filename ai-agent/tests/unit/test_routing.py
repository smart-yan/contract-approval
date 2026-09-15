"""Conditional Edge 判断函数的真值表。

条件边是 Graph 里唯一"由数据决定结构"的地方，因此它必须能被单独测试 ——
这也是把它提成纯函数（而不是写进节点内部）的直接收益。
"""

from __future__ import annotations

import pytest

from app.graph.edges.routing import (
    route_after_llm_review,
    route_after_parse,
    route_after_validate,
)
from app.schemas.document import ParseResult


# --------------------------------------------------------------------------- #
# validate_file 之后
# --------------------------------------------------------------------------- #
def test_routes_to_continue_when_file_valid() -> None:
    assert route_after_validate({"file_valid": True}) == "continue"


def test_routes_to_stop_when_file_invalid() -> None:
    assert route_after_validate({"file_valid": False}) == "stop"


def test_routes_to_stop_when_flag_missing() -> None:
    """``file_valid`` 缺失（节点没跑 / 键名写错）时不能默认放行 —— 门禁必须 fail-closed。"""
    assert route_after_validate({}) == "stop"


def test_routes_to_stop_when_flag_is_not_true() -> None:
    """只有**严格等于 True** 才继续，避免"真值"被误当成校验通过。"""
    assert route_after_validate({"file_valid": False, "validation_errors": ["x"]}) == "stop"


# --------------------------------------------------------------------------- #
# parse_document 之后
# --------------------------------------------------------------------------- #
def _parsed(status: str) -> ParseResult:
    return ParseResult(status=status, parser="DocxParser", source_file_type="DOCX")


@pytest.mark.parametrize("status", ["PARSED", "EMPTY"])
def test_parse_continues_on_usable_document(status: str) -> None:
    assert route_after_parse({"parse_result": _parsed(status)}) == "continue"


def test_parse_stops_when_parsing_failed() -> None:
    """核心不变量：解析失败就必须结束工作流，不能让后续节点拿到一份空文档往下跑。"""
    state = {
        "file_valid": True,
        "error_code": "PARSE_FAILED",
        "parse_result": _parsed("FAILED"),
    }

    assert route_after_parse(state) == "stop"


def test_parse_stops_when_result_missing() -> None:
    """fail-closed：没跑过解析（或键名写错）时不能默认往下走。"""
    assert route_after_parse({}) == "stop"
    assert route_after_parse({"parse_result": None}) == "stop"


# --------------------------------------------------------------------------- #
# llm_review 之后（P9-6a）
#
# 两条路**都继续**：区别只是后续节点该不该指望 ``llm_findings``。
# --------------------------------------------------------------------------- #
def test_llm_routes_to_continue_when_it_produced_findings() -> None:
    state = {"llm_findings": [], "rule_risks": []}

    assert route_after_llm_review(state) == "continue"


def test_llm_routes_to_fallback_on_unavailable() -> None:
    state = {"llm_error_code": "LLM_UNAVAILABLE", "llm_error_message": "LLM 未配置"}

    assert route_after_llm_review(state) == "fallback"


def test_llm_routes_to_fallback_on_schema_invalid() -> None:
    state = {"llm_error_code": "LLM_SCHEMA_INVALID", "llm_error_message": "输出不合契约"}

    assert route_after_llm_review(state) == "fallback"


def test_llm_fallback_does_not_look_at_the_fatal_error_channel() -> None:
    """**关键区分**：整次审查的 ``error_code`` 不决定这条分支。

    LLM 挂了不代表这次审查白跑 —— 规则结果仍然完整可用（§9.1：降级为仅规则结果）。
    若这条路由去读 ``error_code``，一次"规则部分照常可用"的审查会被误判成 LLM 失败。
    """
    state = {"error_code": "SOMETHING_ELSE", "llm_findings": [], "rule_risks": []}

    assert route_after_llm_review(state) == "continue"


def test_llm_routes_to_fallback_when_the_node_never_ran() -> None:
    """fail-closed：没确凿拿到 LLM 结论（节点没跑 / 键名写错）就不能按"有结论"往下走。"""
    assert route_after_llm_review({}) == "fallback"
    assert route_after_llm_review({"llm_error_code": None}) == "fallback"
    assert route_after_llm_review({"rule_risks": [object()]}) == "fallback"
