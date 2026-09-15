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

#: ``llm_review`` 之后的分支名（P9-6a）。
#: ``fallback`` = "只用规则结果继续" —— **不是**结束，两条路都会往下走，
#: 只是带着不同的东西（见 :func:`route_after_llm_review`）。
LLMRouteBranch = Literal["continue", "fallback"]


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


def route_after_persist_document(state: ContractReviewState) -> RouteBranch:
    """文档层落库成功才继续规则审查，失败就结束。

    判断依据只有 ``persist_document`` 写在**致命通道**上的 ``error_code`` ——
    它不是 ``llm_error_code`` 那种降级信号：文档层写不回去，后面的规则与 LLM
    产出**同样写不回去**（Backend 的风险接口要求阶段已到 ``CLAUSED``），
    继续跑只是白烧一遍模型调用。

    ``error_code`` 缺失（节点没跑 / 键名写错）时走 ``continue`` ——
    ⚠️ 这一条与 ``route_after_parse`` 的 fail-closed 方向不同，是刻意的：
    本节点成功时**不写任何 State**（结果已经在 Backend 里），因此"没有 error"
    就是它能给出的唯一成功信号。**"节点根本没被接进图"属于图结构问题**，
    由拓扑测试守着，不靠一个状态位兜底。
    """
    return "stop" if state.get("error_code") else "continue"


def route_after_llm_review(state: ContractReviewState) -> LLMRouteBranch:
    """LLM 审查有结论就正常走，降级就走去掉 LLM 结论的那条路。

    判断依据只有 ``llm_review`` 写在**降级通道**上的 ``llm_error_code``
    （不是整次审查的 ``error_code``）—— LLM 挂了不代表这次审查白跑：
    ``rule_risks`` 仍然完整可用（§9.1：降级为"仅规则引擎结果"）。

    两条路**都继续往下走**，区别是后续节点该不该指望 ``llm_findings``：
    ``continue`` 有模型结论，``fallback`` 只有规则结果。

    ``llm_error_code`` 缺失（节点没跑 / 键名写错）时走 ``fallback`` ——
    与其它路由一致，**fail-closed**：没确凿拿到 LLM 结论，就不能按"有结论"往下走。
    （"缺失"不只是"没有错误码"：``llm_findings`` 本身不在 State 里同样说明节点没跑过，
    那时也必须是 fallback —— 否则后续节点会去找一份**根本不存在**的模型结论。）
    """
    if state.get("llm_error_code"):
        return "fallback"
    if state.get("llm_findings") is None:
        return "fallback"
    return "continue"


__all__ = [
    "LLMRouteBranch",
    "RouteBranch",
    "route_after_llm_review",
    "route_after_parse",
    "route_after_persist_document",
    "route_after_validate",
]
