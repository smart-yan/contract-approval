"""``upload_file`` 节点 —— 编排"把合同文件接入系统"这一步。

调用链
-----
::

    upload_file Node  →  ContractIngestTool  →  BackendClient  →  POST /api/v1/contracts
    （编排）             （业务动作）            （HTTP）           （Backend）

节点**只做编排**：把 State 翻译成 Tool 的输入、把 Tool 的输出写回 State。
它不拼 multipart、不碰 httpx、不解析响应体 —— 那些是下面两层的职责。

职责边界（架构红线）
------------------
本节点与它下面的两层都**不实现**：文件校验（扩展名 / MIME / 魔数）、SHA256 计算、
幂等判断、并发竞争处理、孤儿清理。这些 Backend 在 P4 已经做完并有测试覆盖。
Agent 只做一件事：**把文件交给 Backend，再把结果如实带回来**。

失败为什么不抛异常
----------------
"文件格式不支持"是可预期的业务结果，不是程序 bug。把它抛成异常会让整个 Graph
中断，Conditional Edge 也就没有意义了。节点把失败写进 State，
由 ``validate_file`` + Conditional Edge 决定走向。
"""

from __future__ import annotations

import logging
from pathlib import Path

from langgraph.runtime import Runtime

from app.core.errors import AgentErrorCode
from app.graph.context import ReviewContext
from app.graph.state import ContractReviewState
from app.tools.contract_ingest import ContractIngestRequest, ContractIngestTool

logger = logging.getLogger(__name__)

#: 调用 Tool 之前必须齐备的输入。缺失说明调用方用错了 Graph，不必发请求
_REQUIRED_INPUTS: tuple[str, ...] = ("file_path", "contract_no", "title", "contract_type")


async def upload_file(
    state: ContractReviewState,
    runtime: Runtime[ReviewContext],
) -> dict[str, object]:
    """把合同文件交给 Backend 接入，产出合同 / 附件 / 任务三个 ID。

    * 读 State：``file_path`` / ``filename`` / ``content_type`` / ``contract_no`` /
      ``title`` / ``contract_type``
    * 写 State：``contract_id`` / ``file_id`` / ``review_task_id`` / ``sha256`` /
      ``file_type`` / ``file_size`` / ``reused`` / ``task_reused``，
      失败时写 ``error_code`` / ``error_message``
    """
    missing = [name for name in _REQUIRED_INPUTS if not state.get(name)]
    if missing:
        message = f"缺少 Workflow 必需输入：{', '.join(missing)}"
        logger.warning("upload_file 输入不完整 | %s", message)
        return {
            "error_code": AgentErrorCode.AGENT_INPUT_INVALID.value,
            "error_message": message,
        }

    file_path = Path(state["file_path"])
    request = ContractIngestRequest(
        file_path=file_path,
        filename=state.get("filename") or file_path.name,
        content_type=state.get("content_type"),
        contract_no=state["contract_no"],
        title=state["title"],
        contract_type=state["contract_type"],
    )

    # Tool 是无状态的薄封装，随用随建；HTTP 连接池由 ReviewContext 里的
    # BackendClient 持有，不在这里创建
    result = await ContractIngestTool(runtime.context.backend).run(request)

    if not result.ok:
        logger.warning(
            "upload_file 失败 | code=%s message=%s",
            result.error_code,
            result.error_message,
        )
        return {"error_code": result.error_code, "error_message": result.error_message}

    updates = result.business_fields()
    logger.info(
        "upload_file 完成 | contract_id=%s file_id=%s review_task_id=%s reused=%s task_reused=%s",
        updates.get("contract_id"),
        updates.get("file_id"),
        updates.get("review_task_id"),
        updates.get("reused"),
        updates.get("task_reused"),
    )
    return updates


__all__ = ["upload_file"]
