"""审查报告的**渲染输入模型**与渲染基础函数（P12-2）。

职责边界
-------
::

    ReportData（普通数据对象）  →  本模块  →  Markdown 文本（P12-3）

本模块**不接 ORM、不接 Session、不执行 SQL**。它不知道 ``risk_item`` 有哪些列、
不知道表长什么样、也不知道数据是怎么查出来的 —— 那些全在
:mod:`app.services.report_query`。这样切分的收益是具体的：

* 报告内容可以用**纯函数**单测，不需要 MySQL（``tests/unit/test_report_render.py``）
* "报告读了哪些表、怎么隔离"只有一个地方回答（``report_query.py``），
  不会散落进模板字符串里
* 将来换模板 / 加 HTML 输出，数据来源不动

为什么输入模型定义在**渲染侧**而不是查询侧
----------------------------------------
依赖方向只有一条：``report_query`` 导入本模块，本模块**不**导入 ``report_query``。
把 ``ReportData`` 放在查询侧会让渲染层反过来依赖查询模块（进而依赖 SQLAlchemy），
"渲染层不知道数据库"这条边界就只剩口头约定。现在它是 import 图上的事实。

报告是**只读投影**，不是新的事实来源
----------------------------------
这里的每一个字段都直接来自库里的既有列，本模块**不发明**任何内容：

* **不做审查结论** —— 绝不计算 ``PASS`` / ``RECTIFY`` / ``REJECT``。
  综合等级与结论属 §11.2 的**评分**，按 P9-10 的裁决归 Agent，
  Backend 侧既没实现、也不该在渲染时补一个"没人负责的结论"
* **不做风险统计之外的推断** —— :func:`risk_overview` 只是 ``Counter``
  （"有几条 HIGH"是**事实**，"所以该拒绝"是**结论**，两者不能混）
* **不为空字段编造占位内容** —— ``reason`` / ``legal_basis`` / ``clause_id``
  为空时如实返回空，由模板决定怎么显示"没有"
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.core.constants import RiskLevel

__all__ = [
    "UNLINKED_CLAUSE_LABEL",
    "ReportClause",
    "ReportContract",
    "ReportData",
    "ReportFile",
    "ReportMetadataItem",
    "ReportRisk",
    "ReportTask",
    "RiskOverview",
    "clause_index",
    "clause_label",
    "overview_sentence",
    "render_markdown",
    "risk_overview",
]


# --------------------------------------------------------------------------- #
# 渲染输入模型
# --------------------------------------------------------------------------- #
# 全部 frozen：渲染是只读操作，"渲染时顺手改一下数据对象"在类型层面就不可能。
# 集合一律用 tuple 而不是 list —— frozen 只挡住属性赋值，挡不住 ``risks.append(...)``。


@dataclass(frozen=True, slots=True)
class ReportTask:
    """报告头里的审查任务信息。

    ⚠️ 刻意**没有** ``finished_at`` / ``risk_level_final`` / ``conclusion``：

    * ``finished_at`` 与 ``status`` 在 P9-10 的裁决下恒为 ``NULL`` / ``pending``
      （只推 ``current_stage``，不动状态机）。把一个永远是空的字段放进模型，
      模板只能印出一片"—"，读者却会以为"这个时间丢了"
    * ``risk_level_final`` / ``conclusion`` 是 §11.2 的**评分**结果，
      当前恒为 ``NULL``。报告不展示结论（见模块 docstring），因此不取
    """

    task_id: int
    status: str
    current_stage: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReportContract:
    """合同主数据。字段与 ORM 列一一对应，不做任何加工。"""

    contract_id: int
    contract_no: str
    title: str
    contract_type: str
    our_party: str | None
    counterparty: str | None
    amount: Decimal | None
    currency: str | None
    sign_date: date | None
    effective_date: date | None
    expire_date: date | None
    dept: str | None


@dataclass(frozen=True, slots=True)
class ReportFile:
    """被审查的附件 —— 报告的**可追溯性**来自它（文件名 + SHA-256）。"""

    file_id: int
    file_name: str
    file_type: str | None
    sha256: str
    parse_status: str


@dataclass(frozen=True, slots=True)
class ReportMetadataItem:
    """一条提取到的合同信息。

    ⚠️ 没有 ``source_paragraph_index``：它要经 ``document_block`` 换算，
    而报告不查 block（见 ``report_query`` 的说明）。报告用它做**展示**，
    不需要"这条是从第几段提的"这种审计细节。
    """

    field_key: str
    field_label: str
    field_value: str
    value_type: str
    extract_method: str


@dataclass(frozen=True, slots=True)
class ReportClause:
    """一个条款。

    ⚠️ 没有段落区间：那同样要经 ``document_block`` 换算（P11 工作台需要它做高亮，
    报告不需要）。风险与条款的关联靠 ``ReportRisk.clause_id``，不靠段落号反推。
    """

    clause_id: int
    clause_no: str | None
    clause_type: str
    title: str | None
    text: str


@dataclass(frozen=True, slots=True)
class ReportRisk:
    """一条风险。

    ⚠️ ``original_text`` 是 P10 冻结的语义：**命中的原文片段**（Agent 的 ``quote``
    反查结果），**不是**整段原文。报告按这个语义使用它 —— 想展示整段原文就得去查
    ``document_block``，那是另一个决定（当前不做）。
    """

    risk_id: int
    risk_code: str | None
    risk_title: str
    dimension: str
    risk_level: str
    source: str
    reason: str | None
    legal_basis: str | None
    original_text: str | None
    paragraph_index: int | None
    clause_id: int | None
    locator_type: str

    # ------------------------ 人工复核（P13-4） ------------------------ #
    # ⚠️ 这三列是**人工判断**，与上面的 AI 字段同行不同源。报告只在**风险详情**里
    # 展示它们 —— 风险概览仍然按 ``risk_level`` 统计（那是 AI 审查发现的分布），
    # 被驳回的风险**不从计数里扣除**。
    review_status: str
    review_comment: str | None
    reviewed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReportData:
    """一次审查报告的全部数据。

    各集合**可以为空**（空文档、没有风险……），那是正常的报告内容，
    不是错误。只有 ``task`` / ``contract`` / ``file`` 是必须存在的
    —— 它们缺一就意味着报告不该被生成（查询层会在更早的地方拒绝）。
    """

    task: ReportTask
    contract: ReportContract
    file: ReportFile
    metadata: tuple[ReportMetadataItem, ...]
    clauses: tuple[ReportClause, ...]
    risks: tuple[ReportRisk, ...]


# --------------------------------------------------------------------------- #
# 渲染基础函数
# --------------------------------------------------------------------------- #
#: ``risk_item.clause_id`` 为空时的展示文案。
#:
#: ⚠️ 它**不是**"找不到"的兜底，而是**事实**：§7.2 明确 ``clause_id`` 可空
#: （``MISSING`` 类风险——必备条款缺失——本来就没有对应条款）。
#: 也**不**用 ``paragraph_index`` 去条款区间里反推一个 —— 那是猜测，
#: 而库里的 NULL 是确定的事实。
UNLINKED_CLAUSE_LABEL = "未关联具体条款"

#: 三个已知等级，顺序即报告里的展示顺序（高 → 低）。
_LEVEL_ORDER: tuple[str, ...] = (RiskLevel.HIGH.value, RiskLevel.MEDIUM.value, RiskLevel.LOW.value)


@dataclass(frozen=True, slots=True)
class RiskOverview:
    """风险概览 —— **纯事实统计**，不是审查结论。

    ``other`` 是认不出来的等级（数据库里 ``risk_level`` 是自由字符串，
    没有 CHECK 约束）。刻意**单列**而不是并进 LOW：并进去会让一个数据异常
    静默地变成"一条低风险"。有了它，``high + medium + low + other == total``
    这条恒等式在模板里也说得通。
    """

    total: int
    high: int
    medium: int
    low: int
    other: int


def risk_overview(risks: Sequence[ReportRisk]) -> RiskOverview:
    """数一数各等级各有几条。

    ⚠️ 这里**只数数**。"存在 HIGH"是事实；"所以建议拒绝"是结论 ——
    后者是 §11.2 的评分，不在本模块（见模块 docstring）。
    """
    counts = Counter(risk.risk_level for risk in risks)
    high = counts[RiskLevel.HIGH.value]
    medium = counts[RiskLevel.MEDIUM.value]
    low = counts[RiskLevel.LOW.value]
    return RiskOverview(
        total=len(risks),
        high=high,
        medium=medium,
        low=low,
        other=len(risks) - high - medium - low,
    )


def overview_sentence(overview: RiskOverview) -> str:
    """把概览拼成一句话，例如「本次审查共发现 2 项风险，其中 HIGH 2 项。」

    等级用 ``HIGH`` / ``MEDIUM`` / ``LOW`` 原文，**不译成中文** ——
    与前端一致（P11 的风险卡片也是原样显示 Backend 返回值），
    两处口径相同才不会出现"同一条风险两个叫法"。
    """
    if overview.total == 0:
        return "本次审查未发现风险。"

    named = (
        (RiskLevel.HIGH.value, overview.high),
        (RiskLevel.MEDIUM.value, overview.medium),
        (RiskLevel.LOW.value, overview.low),
    )
    parts = [f"{level} {count} 项" for level, count in named if count]
    if overview.other:
        parts.append(f"其他等级 {overview.other} 项")

    return f"本次审查共发现 {overview.total} 项风险，其中 " + "、".join(parts) + "。"


def clause_index(clauses: Sequence[ReportClause]) -> dict[int, ReportClause]:
    """``clause_id`` → 条款。

    只包含**本任务**的条款（查询层按 ``task_id`` 取），因此拿它去查
    另一任务或另一份文件的 ``clause_id`` 必然查不到 —— 那种情况由
    :func:`clause_label` 如实说"信息缺失"，不会张冠李戴。
    """
    return {clause.clause_id: clause for clause in clauses}


def clause_label(clause_id: int | None, clauses_by_id: Mapping[int, ReportClause]) -> str:
    """风险所属条款的展示文案。三种结局各有各的话说：

    ==========================  ==========================================
    ``clause_id`` 为 ``None``   「未关联具体条款」——**事实**，不是缺失
    ``clause_id`` 查不到         「条款信息缺失（#N）」——数据异常，
                                如实说"缺"，**不**改口说成"未关联"
    ``clause_id`` 查得到         条款号 + 标题；两者都为空时退回「条款 #N」
    ==========================  ==========================================
    """
    if clause_id is None:
        return UNLINKED_CLAUSE_LABEL

    clause = clauses_by_id.get(clause_id)
    if clause is None:
        return f"条款信息缺失（#{clause_id}）"

    parts = [part for part in (clause.clause_no, clause.title) if part]
    return " ".join(parts) if parts else f"条款 #{clause.clause_id}"


# --------------------------------------------------------------------------- #
# Markdown 报告（P12-3）
# --------------------------------------------------------------------------- #
#: 表格单元格里"没有值"的统一写法。**只用于表格** —— 长文本字段为空时整行不输出，
#: 因为"风险原因：—"会被读成"原因是没有"，而事实是"没写原因"。
_EMPTY_CELL = "—"

#: ``current_stage`` → 报告里的中文说明。与前端 ``constants/review.ts`` 同一套词。
_STAGE_LABELS: dict[str, str] = {
    "UPLOADED": "已上传",
    "PARSED": "已解析",
    "CLAUSED": "已切分条款",
    "REVIEWED": "审查完成",
}

#: ``risk_item.review_status`` → 报告里的中文说明（P13-4）。
#: 与前端 ``RISK_REVIEW_STATUS_LABELS`` 是同一套词 —— 两边各自维护一份是跨语言的
#: 必然，但**取值必须一致**，否则同一条风险在工作台叫"已驳回"、在报告里叫别的。
_REVIEW_STATUS_LABELS: dict[str, str] = {
    "PENDING": "待复核",
    "CONFIRMED": "已确认",
    "REJECTED": "已驳回",
    "MODIFIED": "已修改",
}


def render_markdown(data: ReportData) -> str:
    """把 :class:`ReportData` 渲染成最终的 Markdown 报告。

    **确定性**：同样的输入必然逐字节得到同样的输出。这是刻意的，而且有一条
    具体约束在守着它 —— 报告里**没有"生成时间"**。加上它会让同一份数据每次
    导出都不同：既没法在测试里断言全文，也没法对两次导出的报告做 diff
    （"这份报告和上次那份哪里不一样"是法务会问的问题，而答案不该是"时间戳变了"）。

    **只读**：不修改 ``data``，也不持有它（返回的是新字符串）。

    ⚠️ 报告只陈述**事实**。它不写审查结论（``PASS`` / ``RECTIFY`` / ``REJECT``
    或任何等价表述），因为综合结论属 §11.2 的评分，当前库里恒为 NULL
    （见模块 docstring）。
    """
    sections = [
        _title_section(data),
        _contract_section(data.contract),
        _task_section(data),
        _overview_section(data.risks),
        _risk_detail_section(data),
        _clause_section(data.clauses),
        _metadata_section(data.metadata),
    ]
    return "\n\n".join(section for section in sections if section) + "\n"


def _title_section(data: ReportData) -> str:
    contract = data.contract
    parts = [
        f"合同编号：{_cell(contract.contract_no)}",
        f"合同名称：{_cell(contract.title)}",
        f"审查任务：#{data.task.task_id}",
    ]
    return "# 合同审查报告\n\n> " + " ｜ ".join(parts)


def _contract_section(contract: ReportContract) -> str:
    rows = [
        ("合同编号", _cell(contract.contract_no)),
        ("合同名称", _cell(contract.title)),
        ("合同类型", _cell(contract.contract_type)),
        ("我方主体", _cell(contract.our_party)),
        ("相对方", _cell(contract.counterparty)),
        ("合同金额", _amount_text(contract.amount, contract.currency)),
        ("签署日", _date_text(contract.sign_date)),
        ("生效日", _date_text(contract.effective_date)),
        ("到期日", _date_text(contract.expire_date)),
        ("送审部门", _cell(contract.dept)),
    ]
    return "## 一、合同基本信息\n\n" + _table(("项目", "内容"), rows)


def _task_section(data: ReportData) -> str:
    task = data.task
    file = data.file
    rows = [
        ("审查任务", f"#{task.task_id}"),
        ("任务状态", _cell(task.status)),
        ("审查阶段", _stage_text(task.current_stage)),
        ("创建时间", _datetime_text(task.created_at)),
        ("被审查附件", _cell(file.file_name)),
        ("附件类型", _cell(file.file_type)),
        ("解析状态", _cell(file.parse_status)),
        # SHA-256 全量给出：报告要能追溯到"审的到底是哪个文件"
        ("附件 SHA-256", _cell(file.sha256)),
    ]
    return "## 二、审查任务信息\n\n" + _table(("项目", "内容"), rows)


def _overview_section(risks: Sequence[ReportRisk]) -> str:
    overview = risk_overview(risks)
    rows = [
        (RiskLevel.HIGH.value, str(overview.high)),
        (RiskLevel.MEDIUM.value, str(overview.medium)),
        (RiskLevel.LOW.value, str(overview.low)),
    ]
    # 认不出来的等级只在真的出现时才列 —— 平时不占版面，出现时不隐藏
    if overview.other:
        rows.append(("其他等级", str(overview.other)))
    rows.append(("合计", str(overview.total)))

    return (
        "## 三、风险概览\n\n"
        + overview_sentence(overview)
        + "\n\n"
        + _table(("风险等级", "数量"), rows)
    )


def _risk_detail_section(data: ReportData) -> str:
    if not data.risks:
        return "## 四、风险明细\n\n本次审查未发现风险。"

    clauses_by_id = clause_index(data.clauses)
    blocks: list[str] = ["## 四、风险明细"]
    for index, risk in enumerate(data.risks, start=1):
        blocks.append(_risk_block(index, risk, clauses_by_id))
    return "\n\n".join(blocks)


def _risk_block(index: int, risk: ReportRisk, clauses_by_id: Mapping[int, ReportClause]) -> str:
    """一条风险的详情。

    长文本字段（原因 / 依据 / 命中原文）**为空时整行不输出** —— 印一行
    "风险原因：—" 会被读成"原因是没有"，而事实是"没有写原因"。
    """
    lines = [
        f"### {index}. [{_cell(risk.risk_level)}] {_cell(risk.risk_title)}",
        "",
        _table(
            ("项目", "内容"),
            [
                ("风险编码", _cell(risk.risk_code)),
                ("审查维度", _cell(risk.dimension)),
                ("风险来源", _cell(risk.source)),
                # ⚠️ 只按 clause_id 解析，**不**用 paragraph_index 去条款区间反推
                ("所属条款", clause_label(risk.clause_id, clauses_by_id)),
                ("定位方式", _cell(risk.locator_type)),
                ("原文段落", _paragraph_text(risk.paragraph_index)),
                # 人工复核状态只是**附加信息**：它不影响上面任何一行，也不影响
                # 报告开头的风险概览（那里的计数按 ``risk_level`` 走，被驳回的
                # 风险照样计入 —— 那个区域表达的是"AI 发现了什么"）
                ("人工复核", _review_status_text(risk.review_status)),
            ],
        ),
    ]

    bullets = []
    for label, value in (
        ("风险原因", risk.reason),
        ("法律依据", risk.legal_basis),
        # ⚠️ 冻结语义：这是**命中的原文片段**（Agent quote 的反查结果），
        # 不是整段原文。本层不做任何"补全成一段"的处理
        ("命中原文", risk.original_text),
    ):
        text = _inline(value)
        if text:
            bullets.append(f"- **{label}**：{text}")

    # ---- 人工复核的补充信息（P13-4）----
    # 「人工风险等级」只在 MODIFIED 时出现：那时库里的 ``risk_level`` 已经被人工
    # 改写，标题上的 ``[LOW]`` 其实是**人工值**。这一行把它的来源讲清楚，
    # 免得读者把它当成 AI 原来的判断。
    # ⚠️ 也就**只有** MODIFIED 时才有这一行 —— 其余状态下等级就是 AI 的，
    # 再标一次反而会让人以为"AI 等级"和"人工等级"是两回事。
    if risk.review_status == "MODIFIED":
        bullets.append(f"- **人工风险等级**：{_cell(risk.risk_level)}")

    comment = _inline(risk.review_comment)
    if comment:
        bullets.append(f"- **复核意见**：{comment}")

    if risk.reviewed_at is not None:
        bullets.append(f"- **复核时间**：{_datetime_text(risk.reviewed_at)}")

    # 空行分隔：表格后面**紧跟**列表时，部分 Markdown 解析器会把列表当成表格的
    # 续行而整段吞掉。一个空行就能避免这个歧义。
    if bullets:
        lines.extend(["", *bullets])

    return "\n".join(lines)


def _clause_section(clauses: Sequence[ReportClause]) -> str:
    if not clauses:
        return "## 五、条款附录\n\n本次审查未切分出条款。"

    blocks: list[str] = ["## 五、条款附录"]
    for index, clause in enumerate(clauses, start=1):
        heading = _clause_heading(clause)
        blocks.append(f"### {index}. {heading}\n\n{clause.text.strip()}")
    return "\n\n".join(blocks)


def _clause_heading(clause: ReportClause) -> str:
    parts = [part for part in (clause.clause_no, clause.title) if part]
    label = " ".join(parts) if parts else f"条款 #{clause.clause_id}"
    return f"{label}（{_cell(clause.clause_type)}）"


def _metadata_section(metadata: Sequence[ReportMetadataItem]) -> str:
    if not metadata:
        return "## 六、合同信息提取结果\n\n本次审查未提取到合同信息。"

    rows = [
        (_cell(item.field_label), _cell(item.field_value), _cell(item.value_type), _cell(item.extract_method))
        for item in metadata
    ]
    return "## 六、合同信息提取结果\n\n" + _table(("字段", "值", "类型", "提取方式"), rows)


# --------------------------------------------------------------------------- #
# 排版小工具
# --------------------------------------------------------------------------- #
def _table(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _cell(value: object) -> str:
    """表格单元格取值。

    ``|`` 一旦出现在内容里就会**撕开表格**（后面的列全部错位），因此转义；
    换行同理，必须压成空格。这不是防御性编程 —— 合同名称、风险标题、
    提取出来的字段值都来自文档，出现竖线是完全可能的。
    """
    if value is None:
        return _EMPTY_CELL
    text = str(value).strip()
    if not text:
        return _EMPTY_CELL
    return text.replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _inline(value: str | None) -> str:
    """列表项里的文本：把换行压平（Markdown 的列表项跨行会截断），空值返回空串。"""
    if not value:
        return ""
    return " ".join(value.split())


def _amount_text(amount: Decimal | None, currency: str | None) -> str:
    """金额：``Decimal`` 直接 ``str()``，**绝不经过 float**（§7.2 禁止 float 表示金额）。"""
    if amount is None:
        return _EMPTY_CELL
    text = str(amount)
    return f"{text} {currency}" if currency else text


def _date_text(value: date | None) -> str:
    return value.isoformat() if value is not None else _EMPTY_CELL


def _datetime_text(value: datetime) -> str:
    """时间统一标注 UTC —— 库里存的是 naive UTC（见 ``app.utils.datetime_utils``）。

    不加这个标注，读者会把它当本地时间，东八区就差 8 小时。
    """
    return value.strftime("%Y-%m-%d %H:%M") + " (UTC)"


def _stage_text(stage: str) -> str:
    """阶段中文名；认不出来的原样显示（**不**兜底成"未知"）。"""
    return f"{_STAGE_LABELS.get(stage, stage)}（{_cell(stage)}）"


def _paragraph_text(paragraph_index: int | None) -> str:
    return f"第 {paragraph_index} 段" if paragraph_index is not None else _EMPTY_CELL


def _review_status_text(status: str) -> str:
    """复核状态的中文名；认不出来的**原样返回**（与 ``_stage_text`` 同一条规矩）。"""
    return _REVIEW_STATUS_LABELS.get(status, status)
