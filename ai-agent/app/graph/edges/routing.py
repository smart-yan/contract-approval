"""Conditional Edge 的判断函数。

为什么需要 Conditional Edge
--------------------------
Graph 里凡是"同一份 State 可能走两条路"的地方，判断都必须**提成函数、声明式连边**。

如果把这个判断写进节点内部（"失败就什么都不做"），Graph 的结构就被藏进了函数体：
流程图画不出来、走向没法单独测试、也无法在后续阶段插入重试或人工介入。

约定
----
这些函数只回答"往哪边走"，**不关心目标节点叫什么** ——
分支名到真实节点（含 ``END``）的映射写在 ``app/graph/builder.py`` 的 ``path_map`` 里。

它们都是**只读 State、无副作用**的纯函数 —— 正因为如此才能被单独测试。
"""

from __future__ import annotations

from typing import Literal

from app.graph.state import ContractReviewState, has_usable_document

#: 两个分支名，供 ``path_map`` 映射到真实节点
RouteBranch = Literal["continue", "stop"]


def route_after_validate(state: ContractReviewState) -> RouteBranch:
    """校验通过则继续解析，否则结束。

    判断依据只有一个：``validate_file`` 写进 State 的 ``file_valid``。

    ``file_valid`` 缺失（节点没跑 / 键名写错）时走 ``stop`` ——
    门禁必须 **fail-closed**，不能默认放行。
    """
    return "continue" if state.get("file_valid") is True else "stop"


def route_after_parse(state: ContractReviewState) -> RouteBranch:
    """解析出可用文档则继续，否则结束当前工作流。

    判断依据只有一个：:func:`~app.graph.state.has_usable_document`。

    为什么必须有这条边
    -----------------
    没有它，"解析失败"就只是 State 里的一个字段，**图本身不知道要停下来** ——
    今天后面是 ``END`` 所以看不出问题，但任何插在 ``parse_document`` 之后的节点
    （P7 的条款识别就是第一个）都会照常拿到一份空文档往下跑，
    把"我们没读出来"当成"合同里没写"。这条边把那个决定**钉在图的结构里**。

    ``parse_result`` 缺失时走 ``stop`` —— 与门禁一致，fail-closed。
    """
    return "continue" if has_usable_document(state) else "stop"


__all__ = ["RouteBranch", "route_after_parse", "route_after_validate"]
