"""Conditional Edge 的判断函数。

为什么需要 Conditional Edge
--------------------------
这是 Graph 里第一次出现"同一份 State 可能走两条路"：上传成功要继续解析，
上传失败必须直接结束。

如果把这个判断写进节点内部（"失败就什么都不做"），Graph 的结构就被藏进了函数体：
流程图画不出来、走向没法单独测试、也无法在后续阶段插入重试或人工介入。
因此判断被提成一个**只读 State、无副作用**的纯函数，由 Graph 声明式地连边。

约定
----
本函数只回答"往哪边走"，**不关心目标节点叫什么** ——
分支名到真实节点（含 ``END``）的映射写在 ``app/graph/builder.py`` 的 ``path_map`` 里。
"""

from __future__ import annotations

from typing import Literal

from app.graph.state import ContractReviewState

#: 两个分支名，供 ``path_map`` 映射到真实节点
RouteBranch = Literal["continue", "stop"]


def route_after_validate(state: ContractReviewState) -> RouteBranch:
    """校验通过则继续解析，否则结束。

    判断依据只有一个：``validate_file`` 写进 State 的 ``file_valid``。

    ``file_valid`` 缺失（节点没跑 / 键名写错）时走 ``stop`` ——
    门禁必须 **fail-closed**，不能默认放行。
    """
    return "continue" if state.get("file_valid") is True else "stop"


__all__ = ["RouteBranch", "route_after_validate"]
