"""报告渲染层的单元测试（**不需要 MySQL**）。

这里钉住三件事：

1. **报告不做审查结论** —— 无论风险怎么组合，输出里都不会出现
   ``PASS`` / ``RECTIFY`` / ``REJECT``。这不是风格偏好：综合结论属 §11.2 的评分，
   按 P9-10 的裁决归 Agent，Backend 侧既没有实现、也不该在渲染时补一个
2. **空字段如实留空** —— ``reason`` / ``legal_basis`` / ``clause_id`` 为空时
   渲染层不编造内容，也不把"未关联条款"和"条款查不到"说成同一件事
3. **渲染不修改输入** —— 模型是 frozen 的，写入在类型层面就失败

⚠️ 本阶段的渲染层只有**基础函数**（概览统计、条款文案）。完整的 Markdown 模板
在 P12-3，到那时本文件再补模板相关的断言。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.constants import RiskLevel
from app.services.report_render import (
    UNLINKED_CLAUSE_LABEL,
    ReportClause,
    ReportContract,
    ReportData,
    ReportFile,
    ReportMetadataItem,
    ReportRisk,
    ReportTask,
    clause_index,
    clause_label,
    overview_sentence,
    render_markdown,
    risk_overview,
)

#: 审查结论的三个取值 —— 报告里**绝不允许**出现（见模块 docstring）
FORBIDDEN_CONCLUSION_TOKENS = ("PASS", "RECTIFY", "REJECT")


def naive_utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """构造一个**固定**的 naive-UTC 时刻。

    与 ``app.utils.datetime_utils.utcnow()`` 同一口径：项目写库一律走 naive UTC
    （MySQL 的 DATETIME 不存时区，塞 aware datetime 会被静默截断）。
    因此这里先取 aware 再摘掉 tzinfo，而不是写一个字面量 —— ruff 的 DTZ001
    拦下后者是对的：看起来像本地时间的字面量正是这个项目最该避免的东西。
    """
    return datetime(year, month, day, hour, minute, tzinfo=UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #
def make_risk(**overrides: object) -> ReportRisk:
    """一条风险；默认值是最"满"的那种，用例按需覆盖成空值。"""
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
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_clause(**overrides: object) -> ReportClause:
    base = ReportClause(
        clause_id=100,
        clause_no="第三条",
        clause_type="IP",
        title="知识产权",
        text="第三条 知识产权……",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_data(**overrides: object) -> ReportData:
    base = ReportData(
        task=ReportTask(
            task_id=42,
            status="pending",
            current_stage="REVIEWED",
            created_at=naive_utc(2026, 9, 15, 10, 0),
        ),
        contract=ReportContract(
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
            expire_date=None,
            dept="法务部",
        ),
        file=ReportFile(
            file_id=7,
            file_name="采购合同-风险版.docx",
            file_type="DOCX",
            sha256="a" * 64,
            parse_status="PARSED",
        ),
        metadata=(
            ReportMetadataItem(
                field_key="counterparty_name",
                field_label="相对方名称",
                field_value="乙方公司",
                value_type="TEXT",
                extract_method="REGEX",
            ),
        ),
        clauses=(make_clause(),),
        risks=(make_risk(),),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _all_renderer_text(data: ReportData) -> str:
    """把渲染层能产出的**全部文字**拼在一起，供"禁止出现结论"的断言使用。

    ⚠️ **P12-3 起 ``render_markdown`` 也在这里**（P12-2 时留的 TODO）。它是报告的
    总出口 —— 只盯着概览句而放过模板正文，等于"报告里不会出现审查结论"这条保证
    漏掉了 95% 的正文。

    以后渲染层再加别的产出函数时，**同样要接进来**。
    """
    overview = risk_overview(data.risks)
    clauses_by_id = clause_index(data.clauses)
    labels = [clause_label(risk.clause_id, clauses_by_id) for risk in data.risks]
    return "\n".join([overview_sentence(overview), *labels, render_markdown(data)])


# --------------------------------------------------------------------------- #
# 1 / 4 / 5 / 6 / 7 / 8：正常数据与空字段
# --------------------------------------------------------------------------- #
def test_a_complete_report_data_carries_every_section() -> None:
    data = make_data()

    assert data.task.task_id == 42
    assert data.contract.contract_no == "HT-2026-001"
    assert data.file.file_name == "采购合同-风险版.docx"
    assert len(data.metadata) == 1
    assert len(data.clauses) == 1
    assert len(data.risks) == 1


@pytest.mark.parametrize("field", ["reason", "legal_basis", "original_text"])
def test_empty_optional_text_stays_empty(field: str) -> None:
    """空字段**原样留空** —— 不填占位符、不写"无"、更不编一句话。"""
    risk = make_risk(**{field: None})

    assert getattr(risk, field) is None


def test_a_risk_without_clause_says_unlinked_not_missing() -> None:
    """``clause_id`` 为空是**事实**（MISSING 类风险本就没有条款），不是"查不到"。"""
    label = clause_label(None, clause_index(make_data().clauses))

    assert label == UNLINKED_CLAUSE_LABEL
    assert "缺失" not in label


def test_a_risk_pointing_at_an_unknown_clause_says_missing_but_does_not_lie() -> None:
    """``clause_id`` 有值却在**本任务**的条款里找不到 —— 是数据异常。

    此时**不能**改口说成"未关联具体条款"（那是假的：它有关联，只是关联坏了），
    也不能编一个条款名出来。
    """
    label = clause_label(999, clause_index(make_data().clauses))

    assert label != UNLINKED_CLAUSE_LABEL
    assert "999" in label


def test_empty_metadata_and_clauses_are_not_errors() -> None:
    """解析失败 / 空文档 → 空集合，报告照样能出（不是异常）。"""
    data = make_data(metadata=(), clauses=(), risks=())

    assert data.metadata == ()
    assert data.clauses == ()
    assert overview_sentence(risk_overview(data.risks)) == "本次审查未发现风险。"


def test_a_clause_without_number_or_title_falls_back_to_its_id() -> None:
    clauses = (make_clause(clause_no=None, title=None),)

    assert clause_label(100, clause_index(clauses)) == "条款 #100"


def test_an_empty_contract_title_is_passed_through_unchanged() -> None:
    """``title`` / ``contract_no`` 在库里是 NOT NULL，空只可能是空字符串。

    渲染层**不补**"未命名合同"这类占位 —— 编一个标题会让一份提取异常的合同
    看起来一切正常。
    """
    data = make_data(contract=replace(make_data().contract, title="", contract_no=""))

    assert data.contract.title == ""
    assert data.contract.contract_no == ""


# --------------------------------------------------------------------------- #
# 2 / 3：风险概览
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        ((), (0, 0, 0, 0)),
        ((RiskLevel.HIGH.value,), (1, 1, 0, 0)),
        (
            (RiskLevel.HIGH.value, RiskLevel.HIGH.value),
            (2, 2, 0, 0),
        ),
        (
            (RiskLevel.HIGH.value, RiskLevel.MEDIUM.value, RiskLevel.LOW.value),
            (3, 1, 1, 1),
        ),
        (
            (RiskLevel.LOW.value, RiskLevel.LOW.value, RiskLevel.MEDIUM.value),
            (3, 0, 1, 2),
        ),
    ],
)
def test_overview_counts_each_level(levels: tuple[str, ...], expected: tuple[int, ...]) -> None:
    overview = risk_overview([make_risk(risk_level=level) for level in levels])

    assert (overview.total, overview.high, overview.medium, overview.low) == expected


def test_a_zero_risk_review_reads_as_no_risk_found() -> None:
    assert overview_sentence(risk_overview([])) == "本次审查未发现风险。"


def test_the_overview_sentence_states_the_counts() -> None:
    sentence = overview_sentence(
        risk_overview(
            [
                make_risk(risk_id=1, risk_level=RiskLevel.HIGH.value),
                make_risk(risk_id=2, risk_level=RiskLevel.HIGH.value),
            ]
        )
    )

    assert sentence == "本次审查共发现 2 项风险，其中 HIGH 2 项。"


def test_an_unknown_level_is_counted_separately_never_folded_into_low() -> None:
    """``risk_level`` 在库里是自由字符串（没有 CHECK 约束）。

    认不出来的等级**单列**而不是并进 LOW —— 并进去会让一个数据异常静默地
    变成"一条低风险"。同时保证 ``high+medium+low+other == total``。
    """
    overview = risk_overview(
        [make_risk(risk_id=1, risk_level="CRITICAL"), make_risk(risk_id=2, risk_level="LOW")]
    )

    assert (overview.total, overview.low, overview.other) == (2, 1, 1)
    assert overview.high + overview.medium + overview.low + overview.other == overview.total
    assert "其他等级 1 项" in overview_sentence(overview)


# --------------------------------------------------------------------------- #
# 9：报告不做审查结论
# --------------------------------------------------------------------------- #
def test_the_renderer_never_produces_a_review_conclusion() -> None:
    """**本文件最重要的一条断言。**

    穷举所有等级组合，输出里都不允许出现 ``PASS`` / ``RECTIFY`` / ``REJECT``。
    综合结论是 §11.2 的评分结果，当前库里恒为 NULL（``risk_level_final`` /
    ``conclusion`` 都没人写）—— 渲染时"顺手"按最高等级推一个出来，
    等于凭空造一个没人负责的业务结论。
    """
    all_levels = [RiskLevel.HIGH.value, RiskLevel.MEDIUM.value, RiskLevel.LOW.value]

    for mask in range(1 << len(all_levels)):
        risks = [make_risk(risk_id=i, risk_level=lv) for i, lv in enumerate(all_levels) if mask >> i & 1]
        text = _all_renderer_text(make_data(risks=tuple(risks)))
        for token in FORBIDDEN_CONCLUSION_TOKENS:
            assert token not in text, f"报告里出现了审查结论 {token}：{text!r}"


def test_the_render_layer_does_not_even_import_the_conclusion_enum() -> None:
    """比"输出里没有"更强的一条：渲染层**根本没有**拿到结论词表的途径。

    ``ReviewConclusion`` 一旦被 import 进来，下一个改动就很容易顺手用上它。
    """
    from app.services import report_render

    assert "ReviewConclusion" not in dir(report_render)
    assert not hasattr(report_render, "ReviewConclusion")


def test_an_unknown_level_never_becomes_a_conclusion_either() -> None:
    text = _all_renderer_text(make_data(risks=(make_risk(risk_level="CRITICAL"),)))

    for token in FORBIDDEN_CONCLUSION_TOKENS:
        assert token not in text


# --------------------------------------------------------------------------- #
# 10：渲染不修改输入
# --------------------------------------------------------------------------- #
def test_the_models_are_frozen() -> None:
    """渲染是只读操作 —— 往 data / risk / clause 上写属性在类型层面就该失败。

    这比"约定不要改"强：约定靠人记，frozen 靠解释器。
    """
    data = make_data()

    with pytest.raises(FrozenInstanceError):
        data.risks[0].risk_title = "改一下"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        data.contract.title = "改一下"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        data.task.status = "completed"  # type: ignore[misc]


def test_rendering_leaves_the_input_untouched() -> None:
    """渲染前后逐字段比对 —— 顺带确认集合是 tuple（frozen 挡不住 ``append``）。"""
    data = make_data(
        risks=tuple(make_risk(risk_id=900 + i, risk_level=lv) for i, lv in enumerate(FORBIDDEN_CONCLUSION_TOKENS)),
    )
    snapshot = repr(data)

    _all_renderer_text(data)
    risk_overview(data.risks)
    clause_index(data.clauses)

    assert repr(data) == snapshot
    assert isinstance(data.risks, tuple)
    assert isinstance(data.clauses, tuple)
    assert isinstance(data.metadata, tuple)
    with pytest.raises(AttributeError):
        data.risks.append(make_risk())  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Markdown 报告（P12-3）
# --------------------------------------------------------------------------- #
def test_a_full_report_has_every_section() -> None:
    markdown = render_markdown(make_data())

    for heading in (
        "# 合同审查报告",
        "## 一、合同基本信息",
        "## 二、审查任务信息",
        "## 三、风险概览",
        "## 四、风险明细",
        "## 五、条款附录",
        "## 六、合同信息提取结果",
    ):
        assert heading in markdown, f"缺少章节：{heading}"

    assert "HT-2026-001" in markdown
    assert "设备采购合同" in markdown
    assert "知识产权归属相对方" in markdown
    assert "第三条 知识产权" in markdown
    assert "相对方名称" in markdown


def test_the_report_starts_with_the_title_heading() -> None:
    assert render_markdown(make_data()).startswith("# 合同审查报告\n")


def test_a_zero_risk_report_says_so_instead_of_listing_nothing() -> None:
    markdown = render_markdown(make_data(risks=()))

    assert "本次审查未发现风险。" in markdown
    # 概览表仍然给出，读者能看到"确实一条都没有"，而不是猜表格去哪了
    assert "| 合计 | 0 |" in markdown


def test_the_overview_table_counts_every_level() -> None:
    risks = (
        make_risk(risk_id=1, risk_level=RiskLevel.HIGH.value),
        make_risk(risk_id=2, risk_level=RiskLevel.HIGH.value),
        make_risk(risk_id=3, risk_level=RiskLevel.MEDIUM.value),
        make_risk(risk_id=4, risk_level=RiskLevel.LOW.value),
    )
    markdown = render_markdown(make_data(risks=risks))

    assert "本次审查共发现 4 项风险，其中 HIGH 2 项、MEDIUM 1 项、LOW 1 项。" in markdown
    assert "| HIGH | 2 |" in markdown
    assert "| MEDIUM | 1 |" in markdown
    assert "| LOW | 1 |" in markdown
    assert "| 合计 | 4 |" in markdown


def test_an_unknown_level_gets_its_own_row() -> None:
    markdown = render_markdown(make_data(risks=(make_risk(risk_level="CRITICAL"),)))

    assert "| 其他等级 | 1 |" in markdown
    assert "| CRITICAL |" not in markdown  # 它不在三个已知等级里，不该被塞进某一行


def test_every_risk_is_rendered_as_its_own_subsection_in_order() -> None:
    risks = tuple(make_risk(risk_id=900 + i, risk_title=f"第{i}条风险") for i in range(3))

    markdown = render_markdown(make_data(risks=risks))

    positions = [markdown.index(f"### {i}. ") for i in (1, 2, 3)]
    assert positions == sorted(positions), "风险明细没有按输入顺序排列"
    assert markdown.index("第0条风险") < markdown.index("第1条风险") < markdown.index("第2条风险")


def test_a_risk_without_a_clause_says_unlinked() -> None:
    markdown = render_markdown(make_data(risks=(make_risk(clause_id=None),)))

    assert UNLINKED_CLAUSE_LABEL in markdown


def test_a_risk_pointing_at_a_missing_clause_says_missing() -> None:
    markdown = render_markdown(make_data(risks=(make_risk(clause_id=999),)))

    assert "条款信息缺失（#999）" in markdown
    assert UNLINKED_CLAUSE_LABEL not in markdown


@pytest.mark.parametrize(
    ("field", "label"),
    [("reason", "风险原因"), ("legal_basis", "法律依据"), ("original_text", "命中原文")],
)
def test_an_empty_text_field_is_omitted_not_printed_as_a_dash(field: str, label: str) -> None:
    """长文本为空时**整行不输出**。

    印一行"风险原因：—"会被读成"原因是没有"，而事实是"这条风险没写原因"。
    """
    markdown = render_markdown(make_data(risks=(make_risk(**{field: None}),)))

    assert f"- **{label}**" not in markdown


def test_the_quote_is_shown_with_its_frozen_meaning() -> None:
    """``original_text`` 是**命中片段**，报告原样展示，不补成整段原文。"""
    markdown = render_markdown(make_data(risks=(make_risk(original_text="知识产权归乙方"),)))

    assert "- **命中原文**：知识产权归乙方" in markdown


def test_empty_metadata_and_clauses_are_stated_not_left_blank() -> None:
    markdown = render_markdown(make_data(metadata=(), clauses=()))

    assert "本次审查未提取到合同信息。" in markdown
    assert "本次审查未切分出条款。" in markdown


def test_the_amount_never_goes_through_float() -> None:
    """``Decimal("1234.50")`` 必须渲染成 ``1234.50``。

    经一次 float 会变成 ``1234.5`` —— 金额少一位精度，而且**不会报错**。
    """
    markdown = render_markdown(make_data())

    assert "1234.50 CNY" in markdown
    assert "1234.5 " not in markdown


def test_the_timestamp_is_labelled_utc() -> None:
    """库里存的是 naive UTC，不标注会被当成北京时间，东八区差 8 小时。"""
    assert "2026-09-15 10:00 (UTC)" in render_markdown(make_data())


def test_a_pipe_in_the_content_does_not_break_the_table() -> None:
    """``|`` 会撕开表格让后面的列错位 —— 合同名称来自文档，出现竖线完全可能。"""
    contract = replace(make_data().contract, title="甲方|乙方 责任划分")
    markdown = render_markdown(make_data(contract=contract))

    # 定位"合同名称"那一**表格行**（表格行以 "| " 开头；标题行里也有同样的文字）
    row = next(
        line
        for line in markdown.splitlines()
        if line.startswith("| ") and "责任划分" in line
    )
    assert r"甲方\|乙方" in row
    # 2 列表格的一行有 3 个**未转义**的竖线分隔符 —— 多一个就说明列被内容撕开了
    assert row.count("|") - row.count(r"\|") == 3


def test_the_report_carries_no_generation_timestamp() -> None:
    """报告里**没有"生成时间"** —— 有它就不是确定性输出，两次导出无法 diff。"""
    markdown = render_markdown(make_data())

    assert "生成时间" not in markdown
    assert "导出时间" not in markdown
    assert "报告时间" not in markdown


def test_rendering_is_deterministic() -> None:
    data = make_data(
        risks=(make_risk(risk_id=1), make_risk(risk_id=2, risk_level=RiskLevel.LOW.value))
    )

    assert render_markdown(data) == render_markdown(data)


def test_rendering_the_report_does_not_touch_the_input() -> None:
    data = make_data(risks=(make_risk(), make_risk(risk_id=901, clause_id=None)))
    snapshot = repr(data)

    render_markdown(data)

    assert repr(data) == snapshot
    assert isinstance(data.risks, tuple)
