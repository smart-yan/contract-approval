"""人工复核的契约与事务边界（P13-1，**不需要 MySQL**）。

这里钉三件在数据库之外就能定死的事：

1. **请求体能出现哪些字段** —— 这是"谁能改什么"的边界，不是风格问题。
   一个能传 ``risk_title`` 的契约，就等于把"人工复核"变成了"人工重写"
2. **状态规则** —— ``PENDING`` 不能作为复核结果；``MODIFIED`` 必须带等级；
   另两种结论不许带等级
3. **SQL 形状** —— 双 id 条件与 ``FOR UPDATE`` 必须真的出现在发出去的语句里

真正的落库行为（迁移、并发、回滚）在 ``tests/integration/test_risk_review_api.py``。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import mysql

from app.core.constants import RiskLevel, RiskReviewStatus
from app.core.errors import ERROR_SPECS, ErrorCode, NotFoundError
from app.schemas.risk import RiskReviewRequest
from app.services.risk_review import _load_risk_for_update

#: **AI 产出的事实字段**，复核接口一个都不许接受（P13 裁决 §二）。
#: 让它们可写就等于允许绕过 AI 重新定义风险 —— 复核与重写是两件事。
PROTECTED_FIELDS = [
    "task_id",
    "contract_id",
    "clause_id",
    "rule_id",
    "risk_code",
    "risk_title",
    "dimension",
    "source",
    "reason",
    "legal_basis",
    "original_text",
    "page_number",
    "paragraph_index",
    "char_start_global",
    "char_end_global",
    "locator_type",
    "anchor_method",
    "anchor_score",
    "occurrence_index",
    "quote_context",
    "confidence",
]


def _payload(**overrides: object) -> dict:
    base = {"review_status": RiskReviewStatus.CONFIRMED.value}
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 1：请求体只接受三个字段
# --------------------------------------------------------------------------- #
def test_the_request_has_exactly_the_three_review_fields() -> None:
    """契约面就是这么大 —— 新增字段会让下面所有"不可改"的断言失效。"""
    assert set(RiskReviewRequest.model_fields) == {"review_status", "review_comment", "risk_level"}


@pytest.mark.parametrize("field", PROTECTED_FIELDS)
def test_the_request_does_not_declare_any_ai_fact_field(field: str) -> None:
    assert field not in RiskReviewRequest.model_fields


@pytest.mark.parametrize("field", PROTECTED_FIELDS)
def test_sending_an_ai_fact_field_is_rejected_not_silently_dropped(field: str) -> None:
    """**必须 422，不能静默忽略。**

    Pydantic 默认忽略未知字段：那样调用方发一个 ``risk_title`` 会拿到 200，
    然后以为改成功了 —— 实际上服务端一个字都没动。这条断言就是防这个的。
    """
    with pytest.raises(ValidationError) as excinfo:
        RiskReviewRequest(**_payload(**{field: "试图篡改"}))

    assert field in str(excinfo.value)


def test_reviewer_id_cannot_be_supplied_by_the_client() -> None:
    """``reviewer_id`` 是服务端的列，且当前恒为 NULL —— 客户端连提都不许提。

    让它可传就等于承认"调用方可以自报身份"，而项目没有登录体系，
    那个身份无从验证。
    """
    with pytest.raises(ValidationError):
        RiskReviewRequest(**_payload(reviewer_id=1))


def test_reviewed_at_cannot_be_supplied_by_the_client() -> None:
    """``reviewed_at`` 由服务端取 UTC 当前时间。"""
    with pytest.raises(ValidationError):
        RiskReviewRequest(**_payload(reviewed_at="2020-01-01T00:00:00"))


# --------------------------------------------------------------------------- #
# 2：状态规则
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status",
    [RiskReviewStatus.CONFIRMED, RiskReviewStatus.REJECTED],
)
def test_confirm_and_reject_are_accepted_without_a_level(status: RiskReviewStatus) -> None:
    request = RiskReviewRequest(**_payload(review_status=status.value))

    assert request.review_status is status
    assert request.risk_level is None


def test_modified_requires_a_new_level() -> None:
    """``MODIFIED`` 的定义就是"法务调整了等级" —— 不带等级与 ``CONFIRMED`` 无法区分。"""
    with pytest.raises(ValidationError, match="risk_level"):
        RiskReviewRequest(**_payload(review_status=RiskReviewStatus.MODIFIED.value))


def test_modified_with_a_level_is_accepted() -> None:
    request = RiskReviewRequest(
        **_payload(review_status=RiskReviewStatus.MODIFIED.value, risk_level=RiskLevel.LOW.value)
    )

    assert request.risk_level is RiskLevel.LOW


@pytest.mark.parametrize(
    "status",
    [RiskReviewStatus.CONFIRMED, RiskReviewStatus.REJECTED],
)
def test_confirm_and_reject_may_not_carry_a_level(status: RiskReviewStatus) -> None:
    """这两种结论的语义里**不包含**等级修订。

    允许顺带改等级，等于给"只想确认一下"的调用方留了一条静默改写等级的路径 ——
    而等级直接决定报告里的风险分布，且**不会报错**。
    """
    with pytest.raises(ValidationError, match="MODIFIED"):
        RiskReviewRequest(**_payload(review_status=status.value, risk_level=RiskLevel.HIGH.value))


def test_pending_is_never_a_valid_review_result() -> None:
    """``PENDING`` 是 AI 产出时的初始状态，把它当复核结果等于抹掉复核痕迹。"""
    with pytest.raises(ValidationError, match="PENDING"):
        RiskReviewRequest(**_payload(review_status=RiskReviewStatus.PENDING.value))


def test_pending_is_rejected_even_with_a_level() -> None:
    """带上等级也不行 —— 拒绝的理由是状态本身，与等级无关。"""
    with pytest.raises(ValidationError, match="PENDING"):
        RiskReviewRequest(
            **_payload(review_status=RiskReviewStatus.PENDING.value, risk_level=RiskLevel.HIGH.value)
        )


@pytest.mark.parametrize("status", [s.value for s in RiskReviewStatus if s is not RiskReviewStatus.PENDING])
def test_every_non_pending_status_is_reachable(status: str) -> None:
    """**这就是 P13 的完整状态矩阵。**

    "已复核的三种状态可互相修改" + "PENDING 只能出不能进" 合起来，
    恰好等于"目标状态是三种之一" —— 因此服务层不需要再读一次当前状态做迁移判定。
    这条断言把这个等价关系钉住：以后有人往 ``RiskReviewStatus`` 加了第五个值，
    它会先炸，而不是让新状态静默地永远用不了。
    """
    level = RiskLevel.LOW.value if status == RiskReviewStatus.MODIFIED.value else None
    request = RiskReviewRequest(**_payload(review_status=status, risk_level=level))

    assert request.review_status.value == status


def test_an_unknown_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RiskReviewRequest(**_payload(review_status="APPROVED"))


# --------------------------------------------------------------------------- #
# 3：SQL 形状（双 id + FOR UPDATE）
# --------------------------------------------------------------------------- #
class _CapturingResult:
    def scalar_one_or_none(self):
        return None


class _CapturingSession:
    """只记录发出去的语句，不连库。"""

    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _CapturingResult()


def _compiled(statement) -> str:
    return str(statement.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


async def _capture(risk_id: int, task_id: int) -> str:
    """让真实的 ``_load_risk_for_update`` 在假 session 上跑一遍，拿回它发出的语句。

    刻意断言抛的是 ``RISK_NOT_FOUND`` —— 说明语句真的被**执行**了并走到了
    "查不到"分支，而不是半路因为假对象不兼容崩在别处（那样断言 SQL 就是空的，
    测试会假装通过）。
    """
    session = _CapturingSession()
    with pytest.raises(NotFoundError) as excinfo:
        await _load_risk_for_update(session, risk_id, task_id)  # type: ignore[arg-type]

    assert excinfo.value.code is ErrorCode.RISK_NOT_FOUND
    assert session.statement is not None
    return _compiled(session.statement)


async def test_the_lookup_locks_the_row() -> None:
    """没有 ``FOR UPDATE`` 就是裸的读-改-写，两个复核者会各自基于旧快照写入。"""
    sql = await _capture(900, 42)

    assert "FOR UPDATE" in sql.upper()


async def test_the_lookup_is_scoped_by_both_ids() -> None:
    """**越权跨任务修改的防线。**

    ``risk_item.id`` 是全局主键，只按它查的话，拿任意一个 ``task_id``
    就能改到别的任务的风险。两个条件必须同时出现在 WHERE 里。
    """
    sql = await _capture(900, 42)

    assert "risk_item.id = 900" in sql
    assert "risk_item.task_id = 42" in sql


# --------------------------------------------------------------------------- #
# 4：错误码登记
# --------------------------------------------------------------------------- #
def test_the_new_error_codes_are_registered_with_the_right_status() -> None:
    """未登记的错误码会静默回退成 500 —— 那会把"没找到"报成"服务挂了"。"""
    assert ERROR_SPECS[ErrorCode.RISK_NOT_FOUND].http_status == 404
    assert ERROR_SPECS[ErrorCode.RISK_REVIEW_NOT_READY].http_status == 409


def test_risk_not_found_does_not_distinguish_foreign_tasks() -> None:
    """``RISK_NOT_FOUND`` 只有一条文案。

    "不存在"与"属于别的任务"共用它 —— 分开表达等于告诉调用方
    "这个 id 在别处是存在的"。
    """
    assert "不存在" in ERROR_SPECS[ErrorCode.RISK_NOT_FOUND].message
