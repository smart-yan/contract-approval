"""回写正文生成器的单元测试（**不需要 MySQL**，P15-1）。

这里钉住四件事：

1. **正文只用真实存在的数据** —— 没有"综合结论"、没有"建议修改为"、
   没有模型/规则集版本、没有生成时间。它们要么在库里恒为 NULL，要么根本没有
   数据来源（``risk_suggestion`` / ``ai_call_log`` 至今零写入）。为了模板好看
   而编一段出来，等于让审批系统收到一条**没人能追溯**的结论
2. **确定性** —— 同样输入逐字节同样输出；风险清单按传入顺序渲染（本层不排序）
3. **NULL 如实留空** —— ``review_comment`` / ``reviewed_at`` 为 NULL 时那两行
   根本不出现，而不是印成"复核意见：—"（那会被读成"没有意见"）
4. **幂等键公式** —— ``sha256(task_id + ":" + content_hash)`` 是跨步骤契约，
   改动它会让所有历史幂等键失效，因此用**写死的期望值**钉住

⚠️ 本文件**不测**"能不能回写"（阶段门禁、是否已回写、审批系统可用性）——
那些不属于渲染层（见 ``writeback_render`` 的模块 docstring）。
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.constants import RiskLevel
from app.services.report_render import ReportContract, ReportRisk, ReportTask
from app.services.writeback_render import (
    WritebackContent,
    compute_content_hash,
    compute_idempotency_key,
    render_writeback,
)

#: 正文里**绝不允许**出现的词 —— 都是当前没有数据来源的东西（见模块 docstring）。
FORBIDDEN_TOKENS = (
    "综合结论",
    "风险等级：",  # 评分器没实现，risk_level_final 恒 NULL
    "建议修改为",  # risk_suggestion 零行、零写入方
    "模型",
    "规则集版本",
    "生成时间",
    "PASS",
    "RECTIFY",
    "REJECT",
)


def naive_utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """固定的 naive-UTC 时刻（与 ``app.utils.datetime_utils.utcnow()`` 同一口径）。"""
    return datetime(year, month, day, hour, minute, tzinfo=UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #
def make_task(**overrides: object) -> ReportTask:
    base = ReportTask(
        task_id=42,
        status="pending",
        current_stage="REVIEWED",
        created_at=naive_utc(2026, 9, 15, 10, 0),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_contract(**overrides: object) -> ReportContract:
    base = ReportContract(
        contract_id=1,
        contract_no="HT-2026-001",
        title="设备采购合同",
        contract_type="PURCHASE",
        our_party="某某科技",
        counterparty="乙方公司",
        amount=Decimal("1234.50"),
        currency="CNY",
        sign_date=date(2026, 9, 15),
        effective_date=date(2026, 10, 1),
        expire_date=date(2027, 9, 30),
        dept="法务部",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_risk(**overrides: object) -> ReportRisk:
    """一条风险；默认是**未复核**（P13 的初始态）。"""
    base = ReportRisk(
        risk_id=900,
        risk_code="IP_OWNER_SUPPLIER_001",
        risk_title="知识产权归属相对方",
        dimension="知识产权",
        risk_level=RiskLevel.HIGH.value,
        source="RULE",
        reason="成果归属供方会限制我方后续使用。",
        legal_basis="《民法典》第八百四十七条",
        original_text="知识产权归乙方",
        paragraph_index=23,
        clause_id=100,
        locator_type="PARAGRAPH",
        review_status="PENDING",
        review_comment=None,
        reviewed_at=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def render(
    risks: tuple[ReportRisk, ...] = (),
    *,
    task: ReportTask | None = None,
    contract: ReportContract | None = None,
) -> WritebackContent:
    return render_writeback(task or make_task(), contract or make_contract(), risks)


# --------------------------------------------------------------------------- #
# Case 1：正常风险数据
# --------------------------------------------------------------------------- #
def test_a_full_review_renders_every_section() -> None:
    content = render((make_risk(),))

    md = content.content_md
    assert md.startswith("## 合同审查意见\n")
    assert "合同编号：HT-2026-001" in md
    assert "审查任务：#42" in md
    # 三节齐全且顺序固定
    assert md.index("### 一、合同信息") < md.index("### 二、审查任务") < md.index("### 三、风险清单")
    # 合同主数据（含金额与日期的格式化：金额不走 float、时间标 UTC）
    assert "| 合同金额 | 1234.50 CNY |" in md
    assert "| 签署日 | 2026-09-15 |" in md
    assert "| 审查阶段 | 审查完成（REVIEWED） |" in md
    assert "| 创建时间 | 2026-09-15 10:00 (UTC) |" in md
    # 风险明细
    assert "#### 1. [HIGH] 知识产权归属相对方" in md
    assert "| 风险编码 | IP_OWNER_SUPPLIER_001 |" in md
    assert "| 原文段落 | 第 23 段 |" in md
    assert "- **风险原因**：成果归属供方会限制我方后续使用。" in md
    assert "- **法律依据**：《民法典》第八百四十七条" in md
    assert "- **命中原文**：知识产权归乙方" in md


def test_the_body_never_contains_data_we_do_not_have() -> None:
    """**本文件最重要的一条**：没有数据来源的东西一个字都不许出现。"""
    md = render((make_risk(),)).content_md

    for token in FORBIDDEN_TOKENS:
        assert token not in md, f"正文里不该出现 {token!r}"


def test_an_unknown_contract_field_is_shown_as_empty_not_invented() -> None:
    content = render((), contract=make_contract(counterparty=None, amount=None, dept=None))

    assert "| 相对方 | — |" in content.content_md
    assert "| 合同金额 | — |" in content.content_md
    assert "| 送审部门 | — |" in content.content_md


def test_a_pipe_in_the_contract_title_does_not_break_the_table() -> None:
    """合同名/风险标题来自文档，含 ``|`` 完全可能 —— 不转义会把表格撕开。"""
    content = render((make_risk(risk_title="甲方|乙方 责任划分"),), contract=make_contract(title="A|B 合同"))

    md = content.content_md
    assert r"A\|B 合同" in md
    assert r"甲方\|乙方 责任划分" in md


# --------------------------------------------------------------------------- #
# Case 2：风险为空
# --------------------------------------------------------------------------- #
def test_a_review_without_risks_says_so_and_stays_deterministic() -> None:
    first = render(())
    second = render(())

    assert "本次审查未发现风险。" in first.content_md
    assert "#### 1." not in first.content_md, "没有风险就不该有明细小节"
    assert first == second


# --------------------------------------------------------------------------- #
# Case 3 / 4：review_comment 的 NULL 与有值
# --------------------------------------------------------------------------- #
def test_a_null_review_comment_prints_no_line_at_all() -> None:
    content = render((make_risk(review_status="CONFIRMED", review_comment=None),))

    assert "复核意见" not in content.content_md
    assert "None" not in content.content_md
    assert "null" not in content.content_md


def test_a_null_reviewed_at_prints_no_time_line() -> None:
    content = render((make_risk(review_status="CONFIRMED", reviewed_at=None),))

    assert "复核时间" not in content.content_md


def test_a_review_comment_lands_in_the_body() -> None:
    content = render(
        (
            make_risk(
                review_status="CONFIRMED",
                review_comment="已与业务确认，接受该条款。",
                reviewed_at=naive_utc(2026, 9, 16, 8, 12),
            ),
        )
    )

    md = content.content_md
    assert "- **复核意见**：已与业务确认，接受该条款。" in md
    assert "- **复核时间**：2026-09-16 08:12 (UTC)" in md


def test_a_multiline_review_comment_is_flattened() -> None:
    """复核意见是自由文本，可能带换行 —— 换行会把 Markdown 列表项截断。"""
    content = render((make_risk(review_status="CONFIRMED", review_comment="第一行\n第二行"),))

    assert "- **复核意见**：第一行 第二行" in content.content_md


# --------------------------------------------------------------------------- #
# Case 5：三种 P13 复核状态
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "label"),
    [("CONFIRMED", "已确认"), ("REJECTED", "已驳回"), ("MODIFIED", "已修改")],
)
def test_each_review_status_shows_up_in_the_risk_table(status: str, label: str) -> None:
    content = render((make_risk(review_status=status),))

    assert f"| 人工复核 | {label} |" in content.content_md


def test_an_unreviewed_risk_reads_as_pending() -> None:
    content = render((make_risk(),))

    assert "| 人工复核 | 待复核 |" in content.content_md


def test_only_modified_gets_the_human_risk_level_line() -> None:
    """MODIFIED 会**就地覆盖** ``risk_level``（P13 Scheme A）——
    那时标题上的 ``[LOW]`` 已经是人工值，必须讲清楚它的来源。其余状态不讲。"""
    modified = render((make_risk(review_status="MODIFIED", risk_level=RiskLevel.LOW.value),))
    confirmed = render((make_risk(review_status="CONFIRMED", risk_level=RiskLevel.LOW.value),))

    assert "- **人工风险等级**：LOW" in modified.content_md
    assert "人工风险等级" not in confirmed.content_md


def test_an_unknown_review_status_is_shown_as_is() -> None:
    """认不出来的状态**原样显示**，不兜底成"未知"（与 P12 同一条规矩）。"""
    content = render((make_risk(review_status="SOMETHING_NEW"),))

    assert "| 人工复核 | SOMETHING_NEW |" in content.content_md


# --------------------------------------------------------------------------- #
# Case 6：同输入 → 同输出（三件套全等）
# --------------------------------------------------------------------------- #
def test_the_same_input_yields_the_same_three_values() -> None:
    risks = (make_risk(), make_risk(risk_id=901, risk_title="未设置付款前置验收条件", risk_level="MEDIUM"))

    first = render(risks)
    second = render(risks)

    assert first.content_md == second.content_md
    assert first.content_hash == second.content_hash
    assert first.idempotency_key == second.idempotency_key


def test_the_hash_is_sha256_of_the_utf8_body() -> None:
    content = render((make_risk(),))

    expected = hashlib.sha256(content.content_md.encode()).hexdigest()
    assert content.content_hash == expected
    assert len(content.content_hash) == 64


# --------------------------------------------------------------------------- #
# Case 7：复核内容变化 → 三个值全变
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "changed",
    [
        {"review_comment": "补一句批注"},
        {"review_status": "REJECTED"},
        {"risk_level": RiskLevel.LOW.value},
        {"reviewed_at": naive_utc(2026, 9, 16, 9, 0)},
    ],
    ids=["复核意见", "复核状态", "风险等级", "复核时间"],
)
def test_changing_the_human_review_changes_all_three_values(changed: dict[str, object]) -> None:
    reviewed: dict[str, object] = {"review_status": "CONFIRMED", "review_comment": "原批注"}

    before = render((make_risk(**reviewed),))
    after = render((make_risk(**{**reviewed, **changed}),))

    assert after.content_md != before.content_md
    assert after.content_hash != before.content_hash
    assert after.idempotency_key != before.idempotency_key


def test_the_same_comment_on_different_tasks_gets_different_keys() -> None:
    """幂等键是 ``task_id + content_hash`` —— 任务号不同，键必然不同。

    ⚠️ 这两个任务的正文本身也不同（正文头里有任务号），因此这一条**不**证明
    "正文相同、任务不同"时的行为；它证明的是键里确实含任务号。
    真正要保证的语义是"**同一任务**、同一内容不重复"，见下一条。
    """
    risks = (make_risk(),)

    first = render(risks, task=make_task(task_id=42))
    other = render(risks, task=make_task(task_id=43))

    assert first.content_md != other.content_md
    assert first.idempotency_key != other.idempotency_key
    # 构造一个"正文相同"的对照：任务号一样时，键才可能相同
    assert compute_idempotency_key(42, "x" * 64) != compute_idempotency_key(43, "x" * 64)


def test_an_empty_change_does_not_change_anything() -> None:
    """反面对照：什么都没改 → 三个值一个都不许变（否则幂等键就是随机的）。"""
    risks = (make_risk(review_status="MODIFIED", review_comment="同一条批注"),)

    assert render(risks).idempotency_key == render(risks).idempotency_key


# --------------------------------------------------------------------------- #
# Case 8：顺序确定性
# --------------------------------------------------------------------------- #
def test_risks_are_rendered_in_the_given_order() -> None:
    """本层**不排序** —— 排序是调用方的展示策略（当前约定是查询层按 ``risk_id`` 升序）。

    这条用例把"不排序"这个决定钉住：如果哪天有人偷偷加了 ``sorted(...)``，
    它会失败，从而必须显式讨论"顺序规则到底归谁"。
    """
    risks = (
        make_risk(risk_id=903, risk_title="第三条风险"),
        make_risk(risk_id=901, risk_title="第一条风险"),
        make_risk(risk_id=902, risk_title="第二条风险"),
    )

    md = render(risks).content_md

    assert md.index("#### 1. [HIGH] 第三条风险") < md.index("#### 2. [HIGH] 第一条风险")
    assert md.index("#### 2. [HIGH] 第一条风险") < md.index("#### 3. [HIGH] 第二条风险")


def test_the_same_risks_in_a_different_order_produce_a_different_body() -> None:
    """顺序是**输入的一部分** —— 因此"同输入同输出"的承诺只在同一序列下成立。"""
    first = make_risk(risk_id=901, risk_title="甲风险")
    second = make_risk(risk_id=902, risk_title="乙风险")

    ordered = render((first, second)).content_md
    swapped = render((second, first)).content_md

    assert ordered != swapped
    assert ordered.index("甲风险") < ordered.index("乙风险")
    assert swapped.index("乙风险") < swapped.index("甲风险")


# --------------------------------------------------------------------------- #
# 幂等键公式（跨步骤契约，用写死的期望值钉住）
# --------------------------------------------------------------------------- #
def test_the_idempotency_key_formula_is_task_id_colon_content_hash() -> None:
    content_hash = "a" * 64

    expected = hashlib.sha256(f"42:{content_hash}".encode()).hexdigest()
    assert compute_idempotency_key(42, content_hash) == expected


def test_the_content_hash_is_independent_of_how_many_times_it_is_called() -> None:
    assert compute_content_hash("正文") == compute_content_hash("正文")
    assert compute_content_hash("正文") != compute_content_hash("正文 ")


def test_the_render_result_is_frozen_and_self_consistent() -> None:
    content = render((make_risk(),))

    assert content.idempotency_key == compute_idempotency_key(42, content.content_hash)
    assert content.content_hash == compute_content_hash(content.content_md)
    with pytest.raises(FrozenInstanceError):
        content.content_md = "改一下"  # type: ignore[misc]


def test_rendering_does_not_mutate_the_inputs() -> None:
    task = make_task()
    contract = make_contract()
    risks = (make_risk(),)

    render_writeback(task, contract, risks)

    assert task == make_task()
    assert contract == make_contract()
    assert risks == (make_risk(),)
