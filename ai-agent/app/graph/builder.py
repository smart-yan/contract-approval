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
    extract_metadata
      │
      ▼
    extract_keywords
      │
      ▼
    persist_document
      │
      ├── 条件边 ── 失败 ──▶ END（文档层是后面所有产出的挂载点，
      │                       写不回去就没有继续跑的意义）
     success
      ▼
    rule_review
      │
      ▼
    llm_review
      │
      ├── 条件边 ── fallback ──┐（LLM 降级：只带规则结果往下走，
      │                          **不是**半路停下）
     continue                   │
      ▼                         ▼
    merge_risks ◀───────────────┘
      │
      ▼
    persist_risks
      │
      ▼
    END（统一风险列表已写回 Backend；评分 / 报告在后续阶段接在这里）

⚠️ **解析后的条件边不是冗余**：它把"解析失败就不该往下走"放在图的结构里。
P7-1 接入第一个理解层节点时，只需要把 ``continue`` 指向 ``identify_clauses``，
``stop`` 保持不动 —— 这正是当初保留那条边的意义。
如果当初直连 ``END``，这里就得重新想一遍"失败该怎么办"。

**仍然不包含**：suggestions / report / 评分 / checkpoint / interrupt /
人工审核 / 数据库写入 —— 它们在后续阶段接在 ``merge_risks`` 之后。

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
from app.graph.edges.routing import (
    route_after_llm_review,
    route_after_parse,
    route_after_persist_document,
    route_after_validate,
)
from app.graph.nodes.extract_keywords import extract_keywords
from app.graph.nodes.extract_metadata import extract_metadata
from app.graph.nodes.identify_clauses import identify_clauses
from app.graph.nodes.llm_review import llm_review
from app.graph.nodes.merge_risks import merge_risks
from app.graph.nodes.parse_document import parse_document
from app.graph.nodes.persist_document import persist_document
from app.graph.nodes.persist_risks import persist_risks
from app.graph.nodes.rule_review import rule_review
from app.graph.nodes.upload_file import upload_file
from app.graph.nodes.validate_file import validate_file
from app.graph.state import ContractReviewState

# ------------------------------- 节点名 ------------------------------- #
#: 节点名同时是流程图上的标签，测试里也用它断言，因此统一定义为常量，避免手写字面量
NODE_UPLOAD_FILE = "upload_file"
NODE_VALIDATE_FILE = "validate_file"
NODE_PARSE_DOCUMENT = "parse_document"
NODE_IDENTIFY_CLAUSES = "identify_clauses"
NODE_EXTRACT_METADATA = "extract_metadata"
NODE_EXTRACT_KEYWORDS = "extract_keywords"
NODE_PERSIST_DOCUMENT = "persist_document"
NODE_RULE_REVIEW = "rule_review"
NODE_LLM_REVIEW = "llm_review"
NODE_MERGE_RISKS = "merge_risks"
NODE_PERSIST_RISKS = "persist_risks"

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

#: ``persist_document`` 之后的分支表（P10-4）。
#: 文档层失败是**致命**的：它的产出（block/clause/metadata）是后面规则与 LLM 结果的
#: 挂载点，写不回去就没有继续跑的意义 —— 直接 ``stop``，不烧后续的模型调用。
DOCUMENT_ROUTES: dict[str, str] = {
    "continue": NODE_RULE_REVIEW,
    "stop": END,
}

#: ``llm_review`` 之后的分支表（P9-6a 定名，P9-9 接上真实目标）。
#: ⚠️ **两条路目前都通向 ``merge_risks``** —— 这正是当初把分支名先定下来的目的：
#: 接后续节点时**只改这张表**，不用碰节点内部，也不用碰路由函数。
#: ``continue`` 带模型结论往下走，``fallback`` 只带规则结果 ——
#: 但**两条都必须走到合并**：LLM 降级的意思是"这次没有模型结论"，
#: 不是"风险合并不用做了"。降级路径上的 ``rule_risks`` 仍然完整可用。
LLM_ROUTES: dict[str, str] = {
    "continue": NODE_MERGE_RISKS,
    "fallback": NODE_MERGE_RISKS,
}


def build_review_graph() -> CompiledStateGraph:
    """装配并编译最小合同审查 Graph。"""
    graph = StateGraph(ContractReviewState, context_schema=ReviewContext)

    # ---- 节点 ----
    graph.add_node(NODE_UPLOAD_FILE, upload_file)
    graph.add_node(NODE_VALIDATE_FILE, validate_file)
    graph.add_node(NODE_PARSE_DOCUMENT, parse_document)
    graph.add_node(NODE_IDENTIFY_CLAUSES, identify_clauses)
    graph.add_node(NODE_EXTRACT_METADATA, extract_metadata)
    graph.add_node(NODE_EXTRACT_KEYWORDS, extract_keywords)
    graph.add_node(NODE_PERSIST_DOCUMENT, persist_document)
    graph.add_node(NODE_RULE_REVIEW, rule_review)
    graph.add_node(NODE_LLM_REVIEW, llm_review)
    graph.add_node(NODE_MERGE_RISKS, merge_risks)
    graph.add_node(NODE_PERSIST_RISKS, persist_risks)

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

    # ---- 理解层：三个能力互不依赖，都只吃 parse_result，顺序只是可读性选择 ----
    graph.add_edge(NODE_IDENTIFY_CLAUSES, NODE_EXTRACT_METADATA)
    graph.add_edge(NODE_EXTRACT_METADATA, NODE_EXTRACT_KEYWORDS)

    # ---- 文档层落库：解析/理解结果写回 Backend（P10-4 接入） ----
    # 必须在规则审查**之前**：风险依赖条款（clause_id 要靠 clause 表解析），
    # 文档层写不回去，后面整条链的产出同样写不回去。
    graph.add_edge(NODE_EXTRACT_KEYWORDS, NODE_PERSIST_DOCUMENT)

    # ---- 分支：文档层成功才继续规则审查，失败直接结束 ----
    graph.add_conditional_edges(
        NODE_PERSIST_DOCUMENT,
        route_after_persist_document,
        DOCUMENT_ROUTES,
    )

    # ---- 规则审查：确定性规则求值 / LLM 审查（P9-6a 接入） ----
    graph.add_edge(NODE_RULE_REVIEW, NODE_LLM_REVIEW)

    # ---- 分支：LLM 有结论 / 降级为"仅规则结果"，两条都继续 ----
    graph.add_conditional_edges(
        NODE_LLM_REVIEW,
        route_after_llm_review,
        LLM_ROUTES,
    )

    # ---- 统一风险：两条来源合并成一份列表（P9-9 接入） ----
    # ---- 落库：把最终风险写回 Backend（P9-10 接入） ----
    graph.add_edge(NODE_MERGE_RISKS, NODE_PERSIST_RISKS)
    graph.add_edge(NODE_PERSIST_RISKS, END)

    return graph.compile()


__all__ = [
    "CONDITIONAL_ROUTES",
    "DOCUMENT_ROUTES",
    "LLM_ROUTES",
    "NODE_EXTRACT_KEYWORDS",
    "NODE_EXTRACT_METADATA",
    "NODE_IDENTIFY_CLAUSES",
    "NODE_LLM_REVIEW",
    "NODE_MERGE_RISKS",
    "NODE_PARSE_DOCUMENT",
    "NODE_PERSIST_DOCUMENT",
    "NODE_PERSIST_RISKS",
    "NODE_RULE_REVIEW",
    "NODE_UPLOAD_FILE",
    "NODE_VALIDATE_FILE",
    "PARSE_ROUTES",
    "build_review_graph",
]
