"""最小合同审查 Graph 的装配。

当前的图
-------
::

    START
      │
      ▼
    upload_file
      │
      ▼
    validate_file
      │
      ├── 条件边 ── invalid ──▶ END
      │
     valid
      ▼
    parse_document
      │
      ├── 条件边 ── FAILED ──▶ END
      │
     PARSED / EMPTY
      ▼
    identify_clauses
      │
      ▼
    END（P8 起这里会接上 rule_review）

⚠️ **解析后的条件边不是冗余**：它把"解析失败就不该往下走"放在图的结构里。
P7-1 接入第一个理解层节点时，只需要把 ``continue`` 指向 ``identify_clauses``，
``stop`` 保持不动 —— 这正是当初保留那条边的意义。
如果当初直连 ``END``，这里就得重新想一遍"失败该怎么办"。

**不包含**：metadata / keywords / risks / suggestions / report /
LLM / Prompt / checkpoint / interrupt / 人工审核 / 数据库写入。

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
from app.graph.edges.routing import route_after_parse, route_after_validate
from app.graph.nodes.identify_clauses import identify_clauses
from app.graph.nodes.parse_document import parse_document
from app.graph.nodes.upload_file import upload_file
from app.graph.nodes.validate_file import validate_file
from app.graph.state import ContractReviewState

# ------------------------------- 节点名 ------------------------------- #
#: 节点名同时是流程图上的标签，测试里也用它断言，因此统一定义为常量，避免手写字面量
NODE_UPLOAD_FILE = "upload_file"
NODE_VALIDATE_FILE = "validate_file"
NODE_PARSE_DOCUMENT = "parse_document"
NODE_IDENTIFY_CLAUSES = "identify_clauses"

# -------------------------- Conditional Edge -------------------------- #
#: 分支名 → 真实目标（``END`` 是 langgraph 的结束哨兵，不是普通节点）
CONDITIONAL_ROUTES: dict[str, str] = {
    "continue": NODE_PARSE_DOCUMENT,
    "stop": END,
}

#: 解析之后的分支表。
#: ``continue`` 指向第一个理解层节点；解析失败走 ``stop`` 直接收尾 ——
#: 否则条款识别会拿到一份空文档，把"我们没读出来"当成"合同里没写"。
PARSE_ROUTES: dict[str, str] = {
    "continue": NODE_IDENTIFY_CLAUSES,
    "stop": END,
}


def build_review_graph() -> CompiledStateGraph:
    """装配并编译最小合同审查 Graph。"""
    graph = StateGraph(ContractReviewState, context_schema=ReviewContext)

    # ---- 节点 ----
    graph.add_node(NODE_UPLOAD_FILE, upload_file)
    graph.add_node(NODE_VALIDATE_FILE, validate_file)
    graph.add_node(NODE_PARSE_DOCUMENT, parse_document)
    graph.add_node(NODE_IDENTIFY_CLAUSES, identify_clauses)

    # ---- 主干 ----
    graph.add_edge(START, NODE_UPLOAD_FILE)
    graph.add_edge(NODE_UPLOAD_FILE, NODE_VALIDATE_FILE)

    # ---- 分支：validate_file 之后由 State 决定继续还是结束 ----
    graph.add_conditional_edges(
        NODE_VALIDATE_FILE,
        route_after_validate,
        CONDITIONAL_ROUTES,
    )

    # ---- 分支：parse_document 之后由 State 决定继续还是结束 ----
    graph.add_conditional_edges(
        NODE_PARSE_DOCUMENT,
        route_after_parse,
        PARSE_ROUTES,
    )

    # ---- 收尾 ----
    graph.add_edge(NODE_IDENTIFY_CLAUSES, END)

    return graph.compile()


__all__ = [
    "CONDITIONAL_ROUTES",
    "NODE_IDENTIFY_CLAUSES",
    "NODE_PARSE_DOCUMENT",
    "NODE_UPLOAD_FILE",
    "NODE_VALIDATE_FILE",
    "PARSE_ROUTES",
    "build_review_graph",
]
