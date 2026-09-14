"""``validate_file`` 节点 —— Graph 的门禁，产出 Conditional Edge 的判断依据。

它校验什么、不校验什么
--------------------
**不校验**：扩展名 / MIME / 魔数 / 大小上限。
这些 Backend 在 P4 已经做过（三层校验 + 流式落盘时的边写边判）。
Agent 再实现一遍只会产生两套会各自漂移的规则 —— 这是职责边界问题，不是懒。

**校验**：这次上传的产出是否**完整到足以继续跑 Workflow**，
即 Backend 是否真的给出了后续节点要用的合同 ID、附件 ID、任务 ID 与 sha256。
它同时是两件事：

* **接口契约检查**：Backend 将来改了成功响应的字段，这里立刻暴露，而不是等到
  几个节点之后报 KeyError
* **流程门禁**：文件不合法 / 上传失败时，把 Graph 拦在解析之前

为什么是纯同步函数
----------------
只读 State、不做任何 IO，因此它可以在任何地方被单独测试，也天然可重放。
"""

from __future__ import annotations

import logging

from app.core.errors import AgentErrorCode
from app.graph.state import ContractReviewState

logger = logging.getLogger(__name__)

#: 继续 Workflow 所必需的 Backend 产出（必须存在且为正整数）
_REQUIRED_RESULT_FIELDS: tuple[str, ...] = ("contract_id", "file_id", "review_task_id")


def validate_file(state: ContractReviewState) -> dict[str, object]:
    """判断当前 State 是否满足继续 Workflow 的条件。

    写 State：``file_valid``（Conditional Edge 读它）与 ``validation_errors``。
    仅在"上传成功但产出不完整"时补写 ``error_code``。
    """
    errors: list[str] = []

    upload_error = state.get("error_code")
    if upload_error:
        detail = state.get("error_message") or ""
        errors.append(f"上传阶段未成功：{upload_error} {detail}".strip())

    for field in _REQUIRED_RESULT_FIELDS:
        value = state.get(field)
        if not isinstance(value, int) or value <= 0:
            errors.append(f"Backend 未返回可用的 {field}")

    if not state.get("sha256"):
        errors.append("Backend 未返回 sha256")

    result: dict[str, object] = {"file_valid": not errors, "validation_errors": errors}

    # 上传本身失败时保留它原来的 error_code（信息量更大，且是 Backend 的稳定错误码）；
    # 只有"上传成功但产出不完整"才归因到 BACKEND_CONTRACT_INCOMPLETE
    if errors and not upload_error:
        result["error_code"] = AgentErrorCode.BACKEND_CONTRACT_INCOMPLETE.value

    logger.info("validate_file 完成 | file_valid=%s errors=%s", not errors, errors)
    return result


__all__ = ["validate_file"]
