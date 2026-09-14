"""core/constants.py 单元测试。

覆盖：枚举字面值与架构文档一致、两张状态迁移矩阵（§6.1 / §6.2）、
以及**枚举白名单**——防止未获批准的枚举被悄悄塞进来。
"""

from __future__ import annotations

import json
from enum import StrEnum

from app.core import constants as C

#: 当前允许存在的枚举全集。新增枚举必须同步修改此白名单，
#: 使"未经裁决就加枚举"这类越界在 CI 中直接暴露。
#:
#: P2-b 曾把其中 6 个（RuleType / UserRole / SuggestionType / ContractSource /
#: AnchorMethod / LLMScene）收缩掉；P3 建立 ORM 后它们重新有了调用方（表列类型），
#: 经架构裁决后恢复，故重新列入白名单。
ALLOWED_ENUMS = {
    # §6 状态机
    "TaskStatus",
    "TaskStage",
    "BlockReasonCode",
    "ParseStatus",  # P4：contract_file.parse_status 的取值集合
    "WritebackStatus",
    # §11 风险
    "RiskLevel",
    "ReviewConclusion",
    "RiskReviewStatus",
    "RiskSource",
    # §7.2 条款与合同
    "ContractType",
    "ClauseType",
    "ExtractMethod",
    "LocatorType",
    "BlockType",
    "ContractSource",
    "AnchorMethod",
    # P3 随 ORM 建模恢复
    "RuleType",
    "SuggestionType",
    "UserRole",
    "LLMScene",
}

#: 仍然**未**登记的枚举。这些表在 P3 未创建（字段定义不完整），
#: 因此其枚举也一并延后 —— 保持"有调用方才登记"的纪律。
DEFERRED_ENUMS: list[str] = []


def test_task_status_matches_architecture_doc_6_1() -> None:
    assert [s.value for s in C.TaskStatus] == [
        "pending",
        "parsing",
        "reviewing",
        "blocked",
        "completed",
    ]


def test_writeback_status_matches_architecture_doc_6_2() -> None:
    assert [s.value for s in C.WritebackStatus] == [
        "not_written",
        "writing",
        "success",
        "failed",
    ]


def test_task_transition_matrix_rules() -> None:
    t = C.TASK_STATUS_TRANSITIONS
    assert C.TaskStatus.PARSING in t[C.TaskStatus.PENDING]
    assert C.TaskStatus.BLOCKED in t[C.TaskStatus.PENDING]
    assert C.TaskStatus.COMPLETED not in t[C.TaskStatus.PENDING]
    assert C.TaskStatus.PENDING in t[C.TaskStatus.BLOCKED]


def test_completed_is_terminal() -> None:
    """§6.1：completed 不可回退，重新审查必须新建任务。"""
    assert C.TASK_STATUS_TRANSITIONS[C.TaskStatus.COMPLETED] == frozenset()


def test_writeback_success_is_terminal_and_failed_can_retry() -> None:
    w = C.WRITEBACK_STATUS_TRANSITIONS
    assert w[C.WritebackStatus.SUCCESS] == frozenset()
    assert C.WritebackStatus.WRITING in w[C.WritebackStatus.FAILED]


def test_every_status_has_a_transition_entry() -> None:
    """矩阵必须覆盖所有状态，避免新增状态时漏登记导致 KeyError。"""
    assert set(C.TASK_STATUS_TRANSITIONS) == set(C.TaskStatus)
    assert set(C.WRITEBACK_STATUS_TRANSITIONS) == set(C.WritebackStatus)


def test_remaining_enum_vocabulary_is_intact() -> None:
    assert [e.value for e in C.RiskLevel] == ["HIGH", "MEDIUM", "LOW"]
    assert [e.value for e in C.ReviewConclusion] == ["PASS", "RECTIFY", "REJECT"]
    assert len(C.RiskReviewStatus) == 4
    assert C.RiskSource.RULE_AND_LLM.value == "RULE+LLM"
    assert len(C.ContractType) == 5
    assert len(C.ClauseType) == 11
    assert [e.value for e in C.ExtractMethod] == ["RULE", "REGEX", "LLM", "MANUAL"]
    assert [e.value for e in C.LocatorType] == ["PAGE", "PARAGRAPH"]
    assert [e.value for e in C.BlockType] == [
        "TITLE",
        "PARAGRAPH",
        "TABLE_ROW",
        "HEADER",
        "FOOTER",
    ]
    assert len(C.BlockReasonCode) == 10
    assert len(C.TaskStage) == 4


def test_p3_restored_enums_are_declared() -> None:
    """P3 经架构裁决恢复的 6 个枚举，其字面值必须与架构文档一致。"""
    assert [e.value for e in C.RuleType] == ["KEYWORD", "REGEX", "EXISTS", "MISSING", "THRESHOLD"]
    assert [e.value for e in C.SuggestionType] == ["REPLACE", "ADD", "DELETE"]
    assert [e.value for e in C.ContractSource] == ["UPLOAD", "APPROVAL_SYSTEM"]
    assert [e.value for e in C.AnchorMethod] == [
        "CLAUSE_SCOPED",
        "BLOCK_SCOPED",
        "DOC_GLOBAL",
        "CLAUSE_FALLBACK",
    ]
    assert [e.value for e in C.LLMScene] == [
        "CLAUSE_REVIEW",
        "METADATA_EXTRACT",
        "SUGGESTION",
        "SUMMARY",
    ]
    # §2.4.2 三个角色，字面值为小写（与 §7.2 的 role 列一致）
    assert [e.value for e in C.UserRole] == ["legal", "biz", "admin"]


def test_deferred_enums_are_not_declared() -> None:
    still_there = [name for name in DEFERRED_ENUMS if hasattr(C, name)]
    assert not still_there, f"these enums belong to later phases: {still_there}"


def test_module_declares_exactly_the_whitelisted_enums() -> None:
    """白名单校验：多一个枚举就失败（防止远期枚举悄悄回流）。

    注意排除 `StrEnum` 自身 —— 它由 `from enum import StrEnum` 引入模块命名空间，
    也是 `StrEnum` 的子类。
    """
    declared = {
        name
        for name, value in vars(C).items()
        if isinstance(value, type)
        and issubclass(value, StrEnum)
        and value is not StrEnum
        and not name.startswith("_")
    }
    assert declared == ALLOWED_ENUMS, (
        f"unexpected: {declared - ALLOWED_ENUMS} / missing: {ALLOWED_ENUMS - declared}"
    )


def test_str_enum_serializes_as_plain_string() -> None:
    """StrEnum 成员即 str，可直接 JSON 序列化、可直接与字符串比较。"""
    assert isinstance(C.TaskStatus.PENDING, str)
    assert json.dumps({"status": C.TaskStatus.PENDING}) == '{"status": "pending"}'
    assert C.TaskStatus.PENDING == "pending"
