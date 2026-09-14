"""Conditional Edge 判断函数的真值表。

条件边是 Graph 里唯一"由数据决定结构"的地方，因此它必须能被单独测试 ——
这也是把它提成纯函数（而不是写进节点内部）的直接收益。
"""

from __future__ import annotations

from app.graph.edges.routing import route_after_validate


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
