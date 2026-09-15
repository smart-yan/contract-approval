"""合同查询的纯逻辑（P11-3，**不需要 MySQL**）。

这里只测一件事：``current_stage`` → ``progress`` 的映射。

它值得单独测，是因为这个映射**不来自数据库** —— ``review_task.progress``
那一列从来没有被写过（恒为 0），API 返回的进度是推导出来的展示口径。
推导逻辑必须集中一处且覆盖全部阶段，否则新增阶段时会静默返回 0，
前端进度条会一直停在起点而不报错。
"""

from __future__ import annotations

import pytest

from app.core.constants import TaskStage
from app.services.contract_query import progress_for_stage


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (TaskStage.UPLOADED, 0),
        (TaskStage.PARSED, 30),
        (TaskStage.CLAUSED, 60),
        (TaskStage.REVIEWED, 100),
    ],
)
def test_progress_reflects_the_stage(stage: TaskStage, expected: int) -> None:
    assert progress_for_stage(stage.value) == expected


def test_progress_covers_every_known_stage() -> None:
    """**这张表必须覆盖 ``TaskStage`` 的全部取值**（键集合逐一对齐）。

    它是本步唯一的"守门"断言：以后有人往 ``TaskStage`` 里加了新阶段却忘了给进度，
    ``progress_for_stage`` 会落到"未知阶段按 0"的分支 —— 那不会报错，
    只会让前端进度条**静默地**卡在起点。这条用例会先炸。

    ⚠️ 刻意比对**键集合**而不是"值是否为 0"：``UPLOADED`` 本来就合法地映射到 0，
    用值来判断会把"缺失"与"合法映射到 0"混为一谈。
    """
    from app.services.contract_query import _STAGE_PROGRESS

    assert set(_STAGE_PROGRESS) == {stage.value for stage in TaskStage}


def test_progress_is_monotonic_along_the_pipeline() -> None:
    """阶段是按顺序推进的，进度不能倒退 —— 否则前端进度条会来回跳。"""
    ordered = [
        TaskStage.UPLOADED.value,
        TaskStage.PARSED.value,
        TaskStage.CLAUSED.value,
        TaskStage.REVIEWED.value,
    ]

    values = [progress_for_stage(stage) for stage in ordered]

    assert values == sorted(values), f"进度不单调：{values}"
    assert values[0] == 0 and values[-1] == 100, "两端应当正好是 0% 与 100%"


def test_an_unknown_stage_falls_back_to_zero_without_raising() -> None:
    """未知阶段**不抛异常**：查询接口不该因为一行脏数据整个 500。

    它按 0 处理并记 warning —— 宁可前端显示"还没开始"，也不要让列表页打不开。
    """
    assert progress_for_stage("SOMETHING_NEW") == 0
