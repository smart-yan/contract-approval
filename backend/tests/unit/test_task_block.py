"""任务阻塞的**纯逻辑**（P14-4，不需要 MySQL）。

这里钉两件在数据库之外就能定死的事：

1. **状态机是唯一权威** —— "哪些状态能被阻塞"这个判断必须**问矩阵**，
   而不是在服务里另写一张白名单。矩阵改了、这里自动跟着改
2. **阻塞原因必须是登记过的枚举** —— 该列没有 CHECK 约束，校验只能在应用层做

真正的落库行为（行锁、事务、字段边界）在 ``tests/integration/test_task_block_api.py``。
"""

from __future__ import annotations

import pytest

from app.core.constants import TASK_STATUS_TRANSITIONS, BlockReasonCode, TaskStatus
from app.core.errors import ConflictError, ErrorCode, ValidationError
from app.services.task_block import _ensure_reason_code_known, _ensure_transition_allowed

TASK_ID = 42


# --------------------------------------------------------------------------- #
# 1：状态迁移全部由 §6.1 的矩阵决定
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [TaskStatus.PENDING, TaskStatus.PARSING, TaskStatus.REVIEWING])
def test_blockable_states_are_exactly_the_ones_the_matrix_allows(status: TaskStatus) -> None:
    """可阻塞的状态**就是矩阵里有 BLOCKED 的那些** —— 不是这里再列一遍。"""
    assert TaskStatus.BLOCKED in TASK_STATUS_TRANSITIONS[status]

    assert _ensure_transition_allowed(TASK_ID, status.value) is None


@pytest.mark.parametrize("status", [TaskStatus.BLOCKED, TaskStatus.COMPLETED])
def test_non_blockable_states_are_refused(status: TaskStatus) -> None:
    """``blocked`` 不能重复阻塞；``completed`` 是终态。

    — ``blocked``：矩阵里它只指向 ``pending``（要先被"重新审查"才能再阻塞）
    — ``completed``：终态，矩阵里是空集
    """
    with pytest.raises(ConflictError) as excinfo:
        _ensure_transition_allowed(TASK_ID, status.value)

    assert excinfo.value.code is ErrorCode.INVALID_STATE_TRANSITION
    assert excinfo.value.details == {"task_id": TASK_ID, "current_status": status.value}


def test_an_unknown_status_is_refused_too() -> None:
    """不认得的当前状态一律拒绝 —— **白名单，不是黑名单**。

    黑名单（"只要不是 blocked/completed 就放行"）在以后新增状态时会静默放行一个
    谁都没想清楚的状态。
    """
    with pytest.raises(ConflictError):
        _ensure_transition_allowed(TASK_ID, "SOMETHING_NEW")


def test_the_decision_is_taken_from_the_matrix_not_from_a_local_list() -> None:
    """**这条断言防的是"有人把矩阵抄了一份到服务里"。**

    逐个状态比对"服务放行与否"与"矩阵允许与否"—— 两者必须始终一致。
    哪天矩阵加了新状态，而服务里另有一张表没跟着改，这里会先炸。
    """
    for status in TaskStatus:
        allowed_by_matrix = TaskStatus.BLOCKED in TASK_STATUS_TRANSITIONS.get(status, frozenset())
        try:
            _ensure_transition_allowed(TASK_ID, status.value)
        except ConflictError:
            allowed_by_service = False
        else:
            allowed_by_service = True

        assert allowed_by_service == allowed_by_matrix, f"{status.value} 的判定与矩阵不一致"


def test_the_new_reason_code_is_registered() -> None:
    """P14-4 新增的 ``AGENT_GRAPH_EXECUTION_FAILED`` 必须真的在枚举里。

    它与 ``INTERNAL_ERROR`` 分开是有意的：那个是 Backend 自己的内部错误，
    这个的责任方在 Agent（后台跑图崩了）—— 混在一起排查时分不清看哪边日志。
    """
    assert BlockReasonCode.AGENT_GRAPH_EXECUTION_FAILED.value == "AGENT_GRAPH_EXECUTION_FAILED"


# --------------------------------------------------------------------------- #
# 2：阻塞原因必须是登记过的枚举
# --------------------------------------------------------------------------- #
def test_every_registered_reason_code_is_accepted() -> None:
    for code in BlockReasonCode:
        assert _ensure_reason_code_known(TASK_ID, code.value) is None


@pytest.mark.parametrize("bogus", ["SOMETHING_NEW", "agent_graph_execution_failed", "", "阻塞"])
def test_an_unregistered_reason_code_is_refused(bogus: str) -> None:
    """⚠️ 该列**没有 CHECK 约束**，不校验的话 §6.1 把它枚举化的三条理由
    （可统计 / 可自愈 / 前端可按键给修复引导）会全部失效。
    """
    with pytest.raises(ValidationError) as excinfo:
        _ensure_reason_code_known(TASK_ID, bogus)

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
