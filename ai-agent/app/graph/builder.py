"""最小合同审查 Graph 的装配。

P5-3 的图（刻意只有这些）
------------------------
::

    START
      │
      ▼
    upload_file
      │
      ▼
    validate_file
      │
      ├── conditional edge ── invalid ──▶ END
      │
     valid
      ▼
    parse_document
      │
      ▼
    END

P5-3 **不包含**：clauses / metadata / keywords / risks / suggestions / report /
LLM / Prompt / Tool Calling / checkpoint / interrupt / 人工审核 / 数据库写入。
这些属于 P5-4 之后的阶段。

LangGraph 版本说明
-----------------
本文件按 **langgraph 1.2.11（v1.x）** 的 API 编写，不沿用 0.x 教程的写法：

* ``StateGraph(state_schema, context_schema=...)``
  —— v1 用 Runtime Context 做依赖注入，而不是把 client 塞进 State 或打补丁到全局
* ``add_conditional_edges(source, path, path_map)``
* ``compile()`` **不传 checkpointer**（P5-3 不做持久化与恢复）

每次调用 ``build_review_graph()`` 都会返回一个新的编译产物；
依赖在 ``ainvoke(..., context=ReviewContext(...))`` 时注入，
因此同一个 Graph 可以用不同的 ``BackendClient`` 反复运行 —— 测试正是这么做的。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graph.context import ReviewContext
from app.graph.edges.routing import route_after_validate
from app.graph.nodes.parse_document import parse_document
from app.graph.nodes.upload_file import upload_file
from app.graph.nodes.validate_file import validate_file
from app.graph.state import ContractReviewState

# ------------------------------- 节点名 ------------------------------- #
#: 节点名同时是流程图上的标签，测试里也用它断言，因此统一定义为常量，避免手写字面量
NODE_UPLOAD_FILE = "upload_file"
NODE_VALIDATE_FILE = "validate_file"
NODE_PARSE_DOCUMENT = "parse_document"

# -------------------------- Conditional Edge -------------------------- #
#: 分支名 → 真实目标（``END`` 是 langgraph 的结束哨兵，不是普通节点）
CONDITIONAL_ROUTES: dict[str, str] = {
    "continue": NODE_PARSE_DOCUMENT,
    "stop": END,
}


def build_review_graph() -> CompiledStateGraph:
    """装配并编译最小合同审查 Graph。"""
    graph = StateGraph(ContractReviewState, context_schema=ReviewContext)

    # ---- 节点 ----
    graph.add_node(NODE_UPLOAD_FILE, upload_file)
    graph.add_node(NODE_VALIDATE_FILE, validate_file)
    graph.add_node(NODE_PARSE_DOCUMENT, parse_document)

    # ---- 主干 ----
    graph.add_edge(START, NODE_UPLOAD_FILE)
    graph.add_edge(NODE_UPLOAD_FILE, NODE_VALIDATE_FILE)

    # ---- 分支：validate_file 之后由 State 决定继续还是结束 ----
    graph.add_conditional_edges(
        NODE_VALIDATE_FILE,
        route_after_validate,
        CONDITIONAL_ROUTES,
    )

    # ---- 收尾 ----
    graph.add_edge(NODE_PARSE_DOCUMENT, END)

    return graph.compile()


__all__ = [
    "CONDITIONAL_ROUTES",
    "NODE_PARSE_DOCUMENT",
    "NODE_UPLOAD_FILE",
    "NODE_VALIDATE_FILE",
    "build_review_graph",
]
