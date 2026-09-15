"""``persist_document`` 节点 —— 把解析/理解结果写回 Backend（P10-4）。

它在流水线上的位置
----------------
::

    extract_keywords → persist_document → rule_review → llm_review
                       → merge_risks → persist_risks → END
                            │
                失败（映射不成立 / Backend 拒绝）→ END

**为什么必须在 ``rule_review`` 之前**
风险依赖条款：``risk_item.clause_id`` 要靠 ``clause`` 表解析，而 Backend 的风险写入接口
硬性要求任务阶段已到 ``CLAUSED``（P10-2 加的前置门禁）。因此文档层必须先成功落库，
否则后面整条审查链的产出都写不回去 —— 那时再失败，代价是白跑一遍规则与 LLM。

节点**只做 State 编解码**，与其它节点同构：

* 读 State：``review_task_id`` / ``parse_result`` / ``clauses`` / ``metadata``
* 调 ``DocumentPersistenceTool`` —— 字段映射与坐标换算在 ``app/tools/document_persistence.py``
* 失败时写 ``error_code`` / ``error_message``

失败语义：**致命通道，fail-closed**
--------------------------------
两条失败路径都写 ``error_code``（整次审查失败），**都不是** ``llm_error_code``
那种降级通道：

* **映射不成立**（``DocumentMappingError``）—— 我们自己的解析/理解产物自相矛盾。
  它在**这里**被接住并翻译成 State 结果，**绝不允许异常冲出 Graph**：
  一次请求内的异常会让调用方拿到 500 而不是一份说得清的失败报告。
* **Backend 拒绝**（409 / 404 / 422 / 连不上）—— 外部结果，``error_code`` 原样带回。

Graph 随后在条件边上 ``stop``，**不进入规则审查**。

成功时写什么：**什么都不写**
--------------------------
与 ``persist_risks``（P9-10）同一口径：文档层的结果已经在 Backend 里，
State 里没有需要新记的事实。成功与否由**条件边**读 ``error_code`` 判定 ——
不新造"已持久化"这类布尔标记（那会多一个可能和 Backend 实际情况不一致的真相源）。

⚠️ 与之相关的已知取舍：路由判据是"有没有 ``error_code``"。
如果这个节点**根本没被接进图**，``error_code`` 自然是空的，路由会 fail-open。
这属于**图结构**问题，由拓扑测试（``extract_keywords → persist_document → rule_review``）
负责，不该靠一个状态位兜底。
"""

from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from app.core.errors import AgentErrorCode
from app.graph.context import ReviewContext
from app.graph.state import ContractReviewState
from app.tools.document_persistence import (
    DocumentMappingError,
    DocumentPersistenceRequest,
    DocumentPersistenceTool,
)

logger = logging.getLogger(__name__)


async def persist_document(
    state: ContractReviewState,
    runtime: Runtime[ReviewContext],
) -> dict[str, object]:
    """把本次的段落 / 条款 / 元数据**一次**写回 Backend。

    * 读 State：``review_task_id`` / ``parse_result`` / ``clauses`` / ``metadata``
    * 写 State：成功时无；失败时 ``error_code`` / ``error_message``

    **不抛业务异常**：映射不成立与 Backend 拒绝都被翻译成 State 结果（见模块 docstring）。

    ⚠️ 输入**只读**：``clauses`` / ``metadata`` / ``risks`` 原样留在 State 里，
    节点不改写它们的任何一个字段。
    """
    task_id = state.get("review_task_id")
    parse_result = state.get("parse_result")
    if not task_id or parse_result is None:
        # 没有任务 ID 就无从写回；没有解析结果就没有可写的文档内容。
        # 两者都是编排/输入异常，不是"这份文档没有内容"。
        missing = [
            name for name, value in (("review_task_id", task_id), ("parse_result", parse_result)) if not value
        ]
        message = f"缺少 Workflow 必需输入：{', '.join(missing)}，文档层结果无法写回 Backend"
        logger.warning("persist_document 输入不完整 | %s", message)
        return {
            "error_code": AgentErrorCode.AGENT_INPUT_INVALID.value,
            "error_message": message,
        }

    clauses = state.get("clauses") or []
    metadata = state.get("metadata") or []

    try:
        result = await DocumentPersistenceTool(runtime.context.backend).run(
            DocumentPersistenceRequest(
                task_id=task_id,
                parse_result=parse_result,
                clauses=clauses,
                metadata=metadata,
            )
        )
    except DocumentMappingError as exc:
        # ⚠️ **必须在这里接住**：这是我们的解析/理解产物自相矛盾（例如条款引用了
        # 不存在的段落号）。让它冲出 Graph 会让调用方收到 500 而不是一份说得清的失败报告，
        # 而且失败原因会淹没在堆栈里。
        logger.error(
            "persist_document 映射失败 | file_id=%s task_id=%s %s",
            state.get("file_id"),
            task_id,
            exc,
        )
        return {
            "error_code": AgentErrorCode.DOCUMENT_MAPPING_INVALID.value,
            "error_message": str(exc),
        }

    if not result.ok:
        logger.warning(
            "persist_document 失败 | file_id=%s task_id=%s blocks=%d clauses=%d code=%s message=%s",
            state.get("file_id"),
            task_id,
            len(parse_result.paragraphs),
            len(clauses),
            result.error_code,
            result.error_message,
        )
        return {
            "error_code": result.error_code or AgentErrorCode.BACKEND_REJECTED.value,
            "error_message": result.error_message or "文档层结果写回 Backend 失败",
        }

    logger.info(
        "persist_document 完成 | file_id=%s task_id=%s parse_status=%s blocks_created=%d "
        "blocks_reused=%d clauses=%d metadata=%d stage=%s",
        state.get("file_id"),
        task_id,
        result.parse_status,
        result.blocks_created,
        result.blocks_reused,
        result.clauses_persisted,
        result.metadata_persisted,
        result.current_stage,
    )
    return {}


__all__ = ["persist_document"]
