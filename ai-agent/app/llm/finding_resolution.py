"""Finding 落地解析：模型输出 → **已定位、已核对**的可用证据（P9-4）。

它在流水线上的位置
----------------
::

    LLMReviewResult（P9-2 的契约，已过 json_guard 校验）
              │
              ▼  本模块（纯函数，不调 LLM）
        ResolvedFinding[]  —— 每条都带着**文档里真实存在的位置**
              │
              ▼  （后续：merge → 统一风险项）

**findings ≠ risks**：finding 是"模型声称发现了什么"，risk 是要展示/落库的风险项。
两者之间隔着定位与合并 —— 本模块只做前者，**不产出风险模型**，
也不碰 ``RuleRisk`` / ``source`` / ``risk_code``（P9-0 已裁决那些暂不放宽）。

三条处置 policy（集中在这里，正是本模块存在的理由）
-----------------------------------------------
========================================  ==========================================
``clause_index`` 越界（模型报了一条          **丢弃这条 finding** —— 它连"说的是哪条
不存在的条款）                               款"都站不住，没有可核对的基础
``quote`` 在该条款里找不到 / 无法唯一确定     **保留**，但降级到条款级（P9-3 的 fallback）
``related_rule_code`` 不在本次规则快照里      **清空该关联**（风险保留）—— 关联是模型的
                                            声称，必须能被核对；核不上就不能留下
========================================  ==========================================

三条的**共同方向**：宁可少一条结论、少一个关联，也不留下一个无法核对的说法。
每次丢弃/清空都写 warning 日志 —— 静默降级是这个项目一直在防的事。
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from app.llm.findings import LLMFinding
from app.schemas.document import Paragraph
from app.schemas.understanding import Clause
from app.understanding.locator import AnchorMethodValue, LocatedPosition, locate_quote

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedFinding:
    """一条**落到了实处**的 finding：模型的说法 + 文档里的位置。

    刻意**不**把 finding 的字段摊平进来：``finding`` 是模型说的，``paragraph_index`` 等
    是 Agent 核出来的 —— 两者来源不同，摊平会让"这个 quote 是谁给的"变得含糊。
    下游（merge）要的是"模型结论 + 可信位置"这个组合。

    * ``finding``：**已核对过**的 finding —— ``related_rule_code`` 里的不实关联已被清空
    * ``paragraph_index`` / ``original_text``：来自 P9-3 的定位结果
    * ``quote``：**一定能在 ``original_text`` 里找到**（降级时二者相等）
    * ``anchor_method``：这次定位是怎么得到的（``CLAUSE_SCOPED`` / ``CLAUSE_FALLBACK``）
    """

    finding: LLMFinding
    paragraph_index: int
    original_text: str
    quote: str
    anchor_method: AnchorMethodValue


def resolve_findings(
    *,
    findings: Sequence[LLMFinding],
    clauses: Sequence[Clause],
    paragraphs: Sequence[Paragraph],
    known_rule_codes: Collection[str] = (),
) -> list[ResolvedFinding]:
    """把模型报的 findings 逐条落到文档上，返回**可用的**那些（顺序保持不变）。

    :param findings: ``json_guard`` 校验通过后的 findings
    :param clauses: ``identify_clauses`` 的输出（定位范围）
    :param paragraphs: ``ParseResult.paragraphs``（段落序号 → 原文）
    :param known_rule_codes: 本次审查所用规则快照里的全部 ``rule_code``。
        ⚠️ **默认空集合 = 一个关联都不接受**：调用方忘了传时，所有
        ``related_rule_code`` 都会被清空。方向是保守的 ——
        丢掉一个关联只会少一条线索，留下一个假关联会让人以为两件事有关。
    :return: 已定位的 findings；``clause_index`` 不可用的那些**不在这里**（已丢弃并记日志）

    不做的事：不调 LLM、不合并、不评分、不碰 State / Graph / 风险模型、
    不引入 ``char_start`` / ``char_end`` / ``anchor_score``。
    """
    resolved: list[ResolvedFinding] = []

    for finding in findings:
        located = locate_quote(
            paragraphs=paragraphs,
            clauses=clauses,
            clause_index=finding.clause_index,
            quote=finding.quote,
            context_before=finding.context_before,
            context_after=finding.context_after,
        )
        if located is None:
            # clause_index 站不住 → 整条丢弃（定位器已记 warning，这里补上"是哪条 finding"）
            logger.warning(
                "丢弃 finding：clause_index=%s 无法定位，risk_title=%r",
                finding.clause_index,
                finding.risk_title,
            )
            continue

        resolved.append(_to_resolved(finding, located, known_rule_codes))

    return resolved


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _to_resolved(
    finding: LLMFinding, located: LocatedPosition, known_rule_codes: Collection[str]
) -> ResolvedFinding:
    return ResolvedFinding(
        finding=_verified_related_rule_code(finding, known_rule_codes),
        paragraph_index=located.paragraph_index,
        original_text=located.original_text,
        quote=located.quote,
        anchor_method=located.anchor_method,
    )


def _verified_related_rule_code(finding: LLMFinding, known_rule_codes: Collection[str]) -> LLMFinding:
    """核对 ``related_rule_code``：**核不上就清空**（返回副本，不就地改）。

    模型说"这条和某规则是同一件事"是一个**声称**，不是事实 —— 它必须能在本次规则快照里
    找到对应的 ``rule_code`` 才算数。核不上就清空，但**风险本身保留**：
    关联丢了只是少一条线索，风险丢了就是漏报。

    （"清了关联之后还要不要合并"是 merge 的事，不在这一层。）
    """
    code = finding.related_rule_code
    if code is None or code in known_rule_codes:
        return finding

    logger.warning(
        "related_rule_code=%r 不在本次规则快照里（共 %d 条规则），已清空该关联；风险保留",
        code,
        len(known_rule_codes),
    )
    return finding.model_copy(update={"related_rule_code": None})


__all__ = ["ResolvedFinding", "resolve_findings"]
