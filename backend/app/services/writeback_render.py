"""审批回写正文的生成（P15-1）。

职责边界
-------
::

    task + contract + risks  →  本模块  →  content_md / content_hash / idempotency_key

本模块是**纯函数**：不接 ORM、不接 Session、不执行 SQL、不发 HTTP、不读时钟。
与 P12 的 ``report_render`` 同一条边界 —— 因此可以完全离线单测
（``tests/unit/test_writeback_render.py``，不需要 MySQL）。

**renderer ≠ writeback gate**（本模块**不做**的判断）
------------------------------------------------
"这次能不能回写"不是渲染问题，本模块**一概不管**：

* ``current_stage`` 是否已到 ``REVIEWED``（``WRITEBACK_NOT_READY``）
* 风险是否都已人工复核完
* 该任务是否已经回写过（``WRITEBACK_ALREADY_SUCCESS``）
* ``writeback_record.status`` 的状态机推进
* 审批系统是否可用（``APPROVAL_SYSTEM_UNAVAILABLE``）

它们属于后续的 writeback service / API。本模块只回答一个问题：

    给定这三份数据，这次回写的正文**应该长什么样**。

为什么用 P12 的输入模型与排版 helper
--------------------------------
``ReportTask`` / ``ReportContract`` / ``ReportRisk`` 就是回写需要的三份投影
（字段逐个来自库里的既有列，且 ``ReportRisk`` 已经带上了 P13 的人工复核三列），
再定义一套同形状的 ``Writeback*`` 数据类只会多出一份会漂移的副本。

排版 helper（``_table`` / ``_cell`` / ``_inline`` / ``_datetime_text`` …）同理：
"表格里的 ``|`` 要转义、换行要压平、时间要标 UTC"这些规则在一个仓库里存在两份，
迟早一份改了对、另一份没改 —— 而且**两边都不会报错**。跨模块用下划线私有名在
本项目有先例（``services/risk_persistence.py`` 就 import 了
``services/rule_catalog._find_effective_rule_set``）。

⚠️ 如果后续要把它提成公共的 ``services/markdown_format.py``，那是一次**触碰 P12**
的整理，需要单独裁决 —— 本步只 import，不改 P12 一行。

确定性
-----
同样输入必然逐字节得到同样的 ``content_md``，因此也有同样的 ``content_hash``。
正文里**没有"生成时间"**（与 P12 报告同一条理由）：加上它，同一份数据每次生成
都不同 —— 幂等键会随之变化，"重复提交不产生第二条"就永远不成立。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from app.services.report_render import (
    ReportContract,
    ReportRisk,
    ReportTask,
    _amount_text,
    _cell,
    _date_text,
    _datetime_text,
    _inline,
    _paragraph_text,
    _review_status_text,
    _stage_text,
    _table,
    overview_sentence,
    risk_overview,
)

__all__ = [
    "WritebackContent",
    "compute_content_hash",
    "compute_idempotency_key",
    "render_writeback",
]

#: 回写正文的标题。与文档 §12 的模板一致。
_TITLE = "## 合同审查意见"

#: ``risk_item.review_status`` 为这个值时，正文多给一行"人工风险等级"：
#: 那一刻库里的 ``risk_level`` 已经是**人工改写过的值**，标题上的 ``[LOW]``
#: 并不是 AI 的判断。把来源讲清楚，免得审批人当成模型结论。
#: （与 P12 报告的 ``_risk_block`` 同一条规则 —— 这是 P13 的既有语义，不是新发明）
_MODIFIED_STATUS = "MODIFIED"


@dataclass(frozen=True, slots=True)
class WritebackContent:
    """一次回写的正文与两个派生值。

    三个字段是一组**不可分割**的事实：``content_hash`` 由 ``content_md`` 算出，
    ``idempotency_key`` 又由 ``task_id + content_hash`` 算出。分开传它们
    （例如只传一个字符串）会让调用方有机会拼出一个不自洽的组合。
    """

    content_md: str
    """审批系统评论区要贴的 Markdown 正文。"""

    content_hash: str
    """``content_md`` 的 SHA-256（小写十六进制）。落 ``writeback_record.content_hash``。"""

    idempotency_key: str
    """本次回写的幂等键。落 ``writeback_record.idempotency_key``（UNIQUE）。"""


def render_writeback(
    task: ReportTask,
    contract: ReportContract,
    risks: Sequence[ReportRisk],
) -> WritebackContent:
    """生成一次审批回写的正文及其派生值。

    :param task: 审查任务（任务号 / 阶段 / 创建时间）
    :param contract: 合同主数据
    :param risks: 本次审查的风险清单。**按传入顺序渲染** —— 本函数不做排序
        （排序是展示策略，不是渲染逻辑；当前调用方的既有约定是查询层按
        ``risk_id`` 升序返回，见 ``report_query``），因此"同样输入 → 同样输出"
        这条确定性由**输入序列相同**保证。

    :returns: :class:`WritebackContent`。**不修改任何入参**（模型都是 frozen 的）。
    """
    sections = [
        _header(task, contract),
        _contract_section(contract),
        _task_section(task),
        _risk_section(risks),
    ]
    content_md = "\n\n".join(section for section in sections if section) + "\n"

    content_hash = compute_content_hash(content_md)
    return WritebackContent(
        content_md=content_md,
        content_hash=content_hash,
        idempotency_key=compute_idempotency_key(task.task_id, content_hash),
    )


def compute_content_hash(content_md: str) -> str:
    """``content_md`` 的 SHA-256（UTF-8 编码，小写十六进制）。

    ⚠️ 编码固定 UTF-8 —— ``str.encode()`` 的默认值，**与平台 locale 无关**
    （这正是这里不写参数的原因：让人一眼看出编码不是可配的）。
    绝不能改成"平台默认编码"：正文是中文，同一份内容在不同机器上会算出不同的
    哈希，幂等键随之失效，于是**同一条意见会被重复写进审批系统**。
    """
    return hashlib.sha256(content_md.encode()).hexdigest()


def compute_idempotency_key(task_id: int, content_hash: str) -> str:
    """``sha256(f"{task_id}:{content_hash}")``（文档 §6.2 的幂等键公式）。

    语义（已冻结）::

        同一 task + 完全相同 content_md  → 同一个 key → 不产生第二条回写
        同一 task + 复核内容变了         → content_md 变 → 新 key → 允许新的一条

    即**"同内容不重复"，不是"一个任务只能有一条"**。这是刻意的：法务补了批注
    就该有一条新的回写记录，而不是被"已经写过了"挡回去。

    ⚠️ ``:`` 是显式分隔符。``task_id`` 是数字、``content_hash`` 是定长十六进制，
    直接相接其实不会歧义；写出来是为了让"拼接规则"在代码里看得见 ——
    后人改动它会让**所有历史幂等键失效**，那是要写测试钉住的东西
    （见 ``tests/unit/test_writeback_render.py``）。
    """
    return hashlib.sha256(f"{task_id}:{content_hash}".encode()).hexdigest()


# --------------------------------------------------------------------------- #
# 各节
# --------------------------------------------------------------------------- #
def _header(task: ReportTask, contract: ReportContract) -> str:
    parts = [
        f"合同编号：{_cell(contract.contract_no)}",
        f"合同名称：{_cell(contract.title)}",
        f"审查任务：#{task.task_id}",
    ]
    return _TITLE + "\n\n> " + " ｜ ".join(parts)


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
    return "### 一、合同信息\n\n" + _table(("项目", "内容"), rows)


def _task_section(task: ReportTask) -> str:
    """审查任务信息。

    ⚠️ 刻意**不输出** ``task.status``：它在 P9-10 的裁决下恒为 ``pending``
    （只推 ``current_stage``、不动状态机），与"审查阶段：审查完成"并排出现，
    读的人只会得出"这单还没审完"—— 而事实是 **AI 审查结果已持久化**
    （P14-4 冻结语义）。文档 §12 的模板里也只有「任务号 / 审查时间」，没有状态。

    ⚠️ 也**没有**附件名与 SHA-256：那要 ``ReportFile``，而本步约定的输入只有
    task / contract / risks（见 ``render_writeback`` 的签名）。可追溯性由
    ``审查任务 #N`` 承担 —— 报告导出里有完整的附件指纹，评论框里塞 64 位哈希
    对审批人没有意义。
    """
    rows = [
        ("审查任务", f"#{task.task_id}"),
        ("审查阶段", _stage_text(task.current_stage)),
        ("创建时间", _datetime_text(task.created_at)),
    ]
    return "### 二、审查任务\n\n" + _table(("项目", "内容"), rows)


def _risk_section(risks: Sequence[ReportRisk]) -> str:
    overview = risk_overview(risks)
    blocks: list[str] = ["### 三、风险清单", overview_sentence(overview)]
    for index, risk in enumerate(risks, start=1):
        blocks.append(_risk_block(index, risk))
    return "\n\n".join(blocks)


def _risk_block(index: int, risk: ReportRisk) -> str:
    """一条风险。

    版式与 P12 报告的 ``_risk_block`` 保持一致（表格在前、补充说明在后），
    但**不复用它**：报告那条还要处理"所属条款"（要 clauses 入参，本模块没有），
    硬套会多出一个永远为空的参数。这里只保留回写需要的字段。

    长文本字段为空时**整行不输出**：印一行"风险原因：—"会被读成"原因是没有"，
    而事实是"没写原因"。
    """
    lines = [
        f"#### {index}. [{_cell(risk.risk_level)}] {_cell(risk.risk_title)}",
        "",
        _table(
            ("项目", "内容"),
            [
                ("风险编码", _cell(risk.risk_code)),
                ("审查维度", _cell(risk.dimension)),
                ("风险来源", _cell(risk.source)),
                ("原文段落", _paragraph_text(risk.paragraph_index)),
                # 人工复核状态只是**附加信息**：它不改变上面任何一行，也不改变
                # 风险清单的概览句（那里数的是 AI 发现了什么，"已驳回"照样计入）
                ("人工复核", _review_status_text(risk.review_status)),
            ],
        ),
    ]

    bullets: list[str] = []
    for label, value in (
        ("风险原因", risk.reason),
        ("法律依据", risk.legal_basis),
        # ⚠️ 冻结语义：这是**命中的原文片段**（Agent quote 的反查结果），
        # 不是整段原文 —— 本层不做任何"补全成一段"的处理（与 P12 一致）
        ("命中原文", risk.original_text),
    ):
        text = _inline(value)
        if text:
            bullets.append(f"- **{label}**：{text}")

    # ---- 人工复核的补充信息（P13）----
    # ⚠️ 只展示**内容**，不展示 reviewer_id：项目没有 sys_user 表、也没有姓名来源，
    # 把一个工号（甚至更糟 —— 编一个名字）写进给外部审批系统看的评论里，
    # 读的人没有任何办法核对它是谁。
    if risk.review_status == _MODIFIED_STATUS:
        bullets.append(f"- **人工风险等级**：{_cell(risk.risk_level)}")

    comment = _inline(risk.review_comment)
    if comment:
        bullets.append(f"- **复核意见**：{comment}")

    if risk.reviewed_at is not None:
        bullets.append(f"- **复核时间**：{_datetime_text(risk.reviewed_at)}")

    # 空行分隔：表格后面紧跟列表时，部分 Markdown 解析器会把列表当成表格的续行
    # 而整段吞掉（与 P12 同一条理由）
    if bullets:
        lines.extend(["", *bullets])

    return "\n".join(lines)
