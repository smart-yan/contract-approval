"""``upload_file`` 节点 —— 通过 Backend API 完成合同文件接入。

职责边界
-------
本节点**不实现**任何文件校验（扩展名 / MIME / 魔数）、SHA256 计算、幂等判断、
并发竞争处理或孤儿清理 —— 这些 Backend 在 P4 已经做完，并由 328 个测试覆盖。
Agent 只做一件事：**把文件交给 Backend，再把结果翻译进 State**。

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

logger = logging.getLogger(__name__)

#: 调用 Backend 之前必须齐备的输入。缺失说明调用方用错了 Graph，不必发请求
_REQUIRED_INPUTS: tuple[str, ...] = ("file_path", "contract_no", "title", "contract_type")

#: 从 Backend 响应里取出、写进 State 的字段。
#: 只取后续节点真正要用的 —— 不把整个响应体倒进 State（State 不是响应缓存）。
_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "contract_id",
    "file_id",
    "review_task_id",
    "sha256",
    "file_type",
    "file_size",
    "reused",  # 文件层幂等
    "task_reused",  # 任务层幂等
)


async def upload_file(
    state: ContractReviewState,
    runtime: Runtime[ReviewContext],
) -> dict[str, object]:
    """把合同文件交给 Backend 接入，产出合同 / 附件 / 任务三个 ID。

    * 读 State：``file_path`` / ``filename`` / ``content_type`` / ``contract_no`` /
      ``title`` / ``contract_type``
    * 写 State：``contract_id`` / ``file_id`` / ``review_task_id`` / ``sha256`` /
      ``file_type`` / ``file_size`` / ``reused``，失败时写 ``error_code`` /
      ``error_message``
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
    outcome = await runtime.context.backend.upload_contract(
        file_path=file_path,
        filename=state.get("filename") or file_path.name,
        content_type=state.get("content_type"),
        contract_no=state["contract_no"],
        title=state["title"],
        contract_type=state["contract_type"],
    )

    if not outcome.ok or outcome.payload is None:
        logger.warning(
            "upload_file 失败 | status=%s code=%s message=%s",
            outcome.status_code,
            outcome.error_code,
            outcome.error_message,
        )
        return {"error_code": outcome.error_code, "error_message": outcome.error_message}

    updates: dict[str, object] = {
        key: outcome.payload[key] for key in _PASSTHROUGH_FIELDS if key in outcome.payload
    }
    logger.info(
        "upload_file 完成 | contract_id=%s file_id=%s review_task_id=%s reused=%s",
        updates.get("contract_id"),
        updates.get("file_id"),
        updates.get("review_task_id"),
        updates.get("reused"),
    )
    return updates


__all__ = ["upload_file"]
