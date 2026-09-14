"""``rule_review`` 节点 —— 编排"逐条跑规则"这一步（P8-2 第二小步）。

调用链
-----
::

    rule_review Node        →  rules.evaluator.evaluate_rule(rule, clauses)
    （State 编解码 + 遍历）      （纯函数：规则 + 条款 → 三态结论）

节点**只做 State 编解码与遍历**，与 ``identify_clauses`` / ``extract_keywords`` 同构。
规则的判断逻辑**一条都不在这里** —— 它是 P8-1 已独立通过 Review 的领域能力
（``app/rules/evaluator.py``）。这里重写一遍就等于把两套会漂移的判定放进系统。

数据流
-----
::

    state["rule_snapshot"]  ┐
                            ├─▶ evaluate_rule(rule, clauses) ─▶ list[RuleEvaluationResult]
    state["clauses"]        ┘        （每条规则一次）
                                              │
                          ┌───────────────────┴───────────────────┐
                          ▼                                       ▼
              rule_evaluations（三态原样）              rule_risks（risks 展平）

三态**原样写回，不做任何折叠**
--------------------------
``EVALUATION_FAILED`` 既不能被改写成 ``NOT_MATCHED``（那是把"算不出来"说成
"合同没问题"，静默漏报），也不能被丢弃（那样它就从结果里消失了）。
它以 ``RuleEvaluationResult`` 的原样形式留在 ``rule_evaluations`` 里，
并且在本节点的日志里单独计数 —— 失败始终**有地方看得见**。

没有规则快照 = **失败**，不是"没有规则"
------------------------------------
``state["rule_snapshot"]`` 缺失（``None``）说明**规则快照没有进入 Workflow** ——
这是前置输入 / 编排的异常，与"这个合同类型没有配置规则"完全不是一件事：

========================================  ==========================================
``rule_snapshot is None``                 **失败**：写 ``error_code`` +
（规则快照根本没进来）                      ``error_message``，不产出任何结论与风险
``RuleSetSnapshot(version=None, rules=[])``  **正常结论**：``rule_evaluations=[]``、
（合同类型没有配置规则集）                     ``rule_risks=[]``、**不写 error**
========================================  ==========================================

**绝不能把两者都表现成"0 风险正常结束"**：那等于告诉调用方"这份合同没有风险"，
而事实是"我们根本没拿到规则"。这与 P8-1 立的三态是同一条原则 ——
"没跑"不等于"没问题"。

失败时不写 ``rule_evaluations`` / ``rule_risks``（**保持缺失**，而不是写成空列表）：
空列表会被读成"跑了，结论是 0 条"，而缺失 + ``error_code`` 才是"没有得到任何结论"。
"""

from __future__ import annotations

import logging

from app.core.errors import AgentErrorCode
from app.graph.state import ContractReviewState
from app.rules import evaluate_rule
from app.rules.schemas import RuleEvaluationStatus

logger = logging.getLogger(__name__)


def rule_review(state: ContractReviewState) -> dict[str, object]:
    """逐条求值 ``rule_snapshot.rules``，把结论与风险写回 State。

    * 读 State：``rule_snapshot``、``clauses``
    * 写 State：``rule_evaluations``、``rule_risks``（成功时）
      / ``error_code``、``error_message``（没拿到规则快照时）

    **不抛异常**：求值器本身对单条规则不抛（P8-1 的契约），
    因此这里也没有需要翻译的失败 —— 某条规则算不出来会以
    ``EVALUATION_FAILED`` 的形式**出现在结果里**，而不是中断整张图。

    唯一中断规则审查的是**输入缺失**（没拿到规则快照）—— 见模块 docstring 的对照表。
    """
    snapshot = state.get("rule_snapshot")
    clauses = state.get("clauses") or []
    # P7-2 已经抽好的元数据**原样递下去** —— 节点不解析它、不转换它。
    # THRESHOLD 规则按 expression["field"] 在里面查值；KEYWORD / REGEX 不看它。
    metadata = state.get("metadata") or []

    if snapshot is None:
        # 规则快照没进 Workflow：这是编排/前置输入的异常，**不是"没有规则"**。
        # 用既有的 error_code / error_message 机制表达（与"调用方没给全输入"同一个码），
        # 而不是新造一套失败体系。刻意**不写** rule_evaluations / rule_risks ——
        # 写空列表会被下游读成"跑了，0 条结论"。
        logger.error(
            "rule_review 缺少输入：State 中没有 rule_snapshot，规则审查未执行 | file_id=%s",
            state.get("file_id"),
        )
        return {
            "error_code": AgentErrorCode.AGENT_INPUT_INVALID.value,
            "error_message": "规则审查缺少必需输入：State 里没有 rule_snapshot（规则集快照）。"
            "这与「该合同类型没有配置规则集」不同 —— 后者是一个 rules=[] 的快照，"
            "会正常执行并得到空结论；这里则是规则快照根本没有进入 Workflow，"
            "因此**没有任何规则被评估过**，不能当作「没有风险」。",
        }

    # 顺序 = snapshot.rules 的顺序（Backend 已按 sort_order ASC, id ASC 排好）。
    # 这里不排序、不筛选、不跳过任何一条规则 —— 包括我们算不出来的那些。
    evaluations = [evaluate_rule(rule, clauses, metadata=metadata) for rule in snapshot.rules]

    # 展平但不重建：RuleRisk 原样搬运，定位信息（paragraph_index / quote）随对象一起走
    risks = [risk for evaluation in evaluations for risk in evaluation.risks]

    failed = [e for e in evaluations if e.status is RuleEvaluationStatus.EVALUATION_FAILED]
    if failed:
        # 失败的规则必须能在日志里被点名 —— 否则"这次审查少跑了几条规则"就没有痕迹
        logger.warning(
            "rule_review 有 %d 条规则无法求值 | file_id=%s rules=%s",
            len(failed),
            state.get("file_id"),
            [f"{e.rule_code}:{e.failure_reason}" for e in failed],
        )

    logger.info(
        "rule_review 完成 | file_id=%s rule_set_version=%s rules=%d matched=%d not_matched=%d "
        "evaluation_failed=%d risks=%d",
        state.get("file_id"),
        snapshot.rule_set_version,
        len(evaluations),
        sum(1 for e in evaluations if e.status is RuleEvaluationStatus.MATCHED),
        sum(1 for e in evaluations if e.status is RuleEvaluationStatus.NOT_MATCHED),
        len(failed),
        len(risks),
    )
    return {"rule_evaluations": evaluations, "rule_risks": risks}


__all__ = ["rule_review"]
