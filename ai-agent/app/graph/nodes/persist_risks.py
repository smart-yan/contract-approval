"""``persist_risks`` 节点 —— 把最终风险结果写回 Backend（P9-10）。

它在流水线上的位置
----------------
::

    rule_review → llm_review → merge_risks → persist_risks → END
                                                │
                                    Agent 的最终风险列表 → Backend

**为什么单独一个节点，而不是塞进 ``merge_risks``**
合并是**纯计算**（给定输入必然得到同一个输出，不碰任何外部状态），
持久化是**有副作用的 IO**。两者的失败语义也完全不同：合并没有失败，
持久化会失败。把它们放进一个节点，"合并"就再也不能被单独测试、
也不再能保证"跑两遍结果一样"。

节点**只做 State 编解码**，与其它节点同构：

* 读 State：``risks``（合并产物）、``review_task_id``（``upload_file`` 给的）
* 调 ``RiskPersistenceTool`` —— 字段映射在 ``app/tools/risk_persistence.py``
* 失败时写 ``error_code`` / ``error_message``

失败语义：**这是一条致命通道，不是降级通道**
------------------------------------------
LLM 挂了可以降级（规则结果还在，§9.1 第 4 道防线）。**风险没能写进 Backend
不能降级** —— State 不落库，进程一结束这次审查的全部结论就没了，
而调用方会拿到一个"看起来成功"的响应。所以这里用的是 ``error_code``
（整次审查失败），**不是** ``llm_error_code`` 那样的第二条降级通道。

成功时写什么：**什么都不写**
--------------------------
持久化成功没有需要记进 State 的新事实：风险已经在 Backend 里，
State 里的 ``risks`` 本来就是它的来源。返回 ``{}`` 而不是伪造一个
"已持久化"标记位 —— 那个标记位迟早会有人问"它和 Backend 里的数据谁说了算"。

"这个节点到底跑没跑"由日志回答（完成时记一条，含写入条数与任务状态）；
若节点被漏接，是**图结构**的问题，由接线测试负责，不该靠一个状态位兜底。
"""

from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from app.core.errors import AgentErrorCode
from app.graph.context import ReviewContext
from app.graph.state import ContractReviewState
from app.tools.risk_persistence import RiskPersistenceRequest, RiskPersistenceTool

logger = logging.getLogger(__name__)


async def persist_risks(
    state: ContractReviewState,
    runtime: Runtime[ReviewContext],
) -> dict[str, object]:
    """把 ``state["risks"]`` 整批写回 Backend，并结束该审查任务。

    * 读 State：``risks``、``review_task_id``
    * 写 State：成功时无；失败时 ``error_code`` / ``error_message``

    **不抛异常**：Backend 的拒绝（任务已写入过 / 规则编码解析不出来）与
    连不上一样，都是要如实写进 State 的结果，而不是需要往上冒的异常。

    ⚠️ 输入**只读**：``risks`` 原样留在 State 里，节点不修改它。
    """
    task_id = state.get("review_task_id")
    if not task_id:
        # 没有任务 ID 就无从写入 —— 这是编排/输入异常，不是"没有风险可写"
        message = "缺少 Workflow 必需输入：State 里没有 review_task_id，风险无法写回 Backend"
        logger.warning("persist_risks 输入不完整 | %s", message)
        return {
            "error_code": AgentErrorCode.AGENT_INPUT_INVALID.value,
            "error_message": message,
        }

    risks = state.get("risks") or []

    result = await RiskPersistenceTool(runtime.context.backend).run(
        RiskPersistenceRequest(task_id=task_id, risks=risks)
    )

    if not result.ok:
        logger.warning(
            "persist_risks 失败 | file_id=%s task_id=%s risks=%d code=%s message=%s",
            state.get("file_id"),
            task_id,
            len(risks),
            result.error_code,
            result.error_message,
        )
        return {
            "error_code": result.error_code or AgentErrorCode.BACKEND_REJECTED.value,
            "error_message": result.error_message or "风险写回 Backend 失败",
        }

    logger.info(
        "persist_risks 完成 | file_id=%s task_id=%s persisted=%d task_status=%s task_stage=%s",
        state.get("file_id"),
        task_id,
        result.persisted,
        result.task_status,
        result.task_stage,
    )
    return {}


__all__ = ["persist_risks"]
