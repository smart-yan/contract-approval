"""Clause Locator：``clause_index`` + ``quote`` → ``paragraph_index``（P9-3）。

职责边界
-------
::

    Clause[] + Paragraph[] + (clause_index, quote, context_before/after)
                          │
                          ▼
                  LocatedPosition（paragraph_index + anchor_method）
                  或 None（这条 finding 不可用，调用方丢弃）

**纯函数**：不发 HTTP、不碰 LLM / Graph / Backend、不做文件 IO。
它只回答一个问题：

    这段原文证据，**落在本文档的哪一段**？

为什么放在 ``understanding/`` 而不是 ``llm/``
------------------------------------------
这是架构文档 §10.5「quote → 坐标反查算法」的最小实现，而 §10 的归属说明把
"坐标系、quote 反查算法"整体划给了 Agent 的**文档理解域**（执行位置从
``backend/app/parsers/`` 移到 ``ai-agent/``），与 LLM 调用无关。
因此本模块**不认识任何 LLM 类型**（不 import ``LLMFinding``）——
它只吃 ``Clause`` / ``Paragraph``，将来 MISSING/EXISTS 类规则要用同一套定位也直接可用。

⚠️ 本轮只做到**段落级**，且只做确定性匹配
---------------------------------------
不实现 ``char_start_global`` / ``char_end_global`` / ``anchor_score`` /
折叠索引 / fuzzy 匹配 / embedding / PDF 页码 —— 这些是 §10.2/§10.5 的完整目标，
P9 的验收口径是"跳转并高亮到对应段落"（与 P6-2 已批准的位置契约一致）。

宁可 fallback，不可错标
--------------------
§10.5 的第一原则。因此：

* 只认**逐字**出现（``quote in text``），不做任何近似匹配
* 上下文消歧用**紧邻**判据（前文以它结尾、后文以它开头），不做相似度打分 ——
  打分就会产生"看起来最像"的候选，那正是错标的来源
* 上下文按**每一个 occurrence 独立判定**：一个段落只有**恰好一个** occurrence
  同时满足全部给定上下文时才算确定（0 个或 ≥2 个都不算）；
  两个上下文必须落在**同一个** occurrence 上，不允许各取一处拼出来
* **没给上下文时不进入 occurrence 级去歧义** —— 定位目标是**段落**，
  同一段里出现几次都指向同一个段落，那仍然算确定
* 无法**唯一**确定时一律 fallback，绝不"挑一个最像的"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from app.schemas.document import Paragraph
from app.schemas.understanding import Clause

logger = logging.getLogger(__name__)

#: 本定位器能产出的两种 ``anchor_method`` —— 它们是 Backend ``AnchorMethod`` 的第 1 与第 4 个取值。
#: 另外两个（``BLOCK_SCOPED`` / ``DOC_GLOBAL``）属于更细的搜索层级，
#: 段落级定位**不会**产出它们，因此这里不提前登记。
ANCHOR_CLAUSE_SCOPED = "CLAUSE_SCOPED"
ANCHOR_CLAUSE_FALLBACK = "CLAUSE_FALLBACK"

AnchorMethodValue = Literal["CLAUSE_SCOPED", "CLAUSE_FALLBACK"]


@dataclass(frozen=True, slots=True)
class LocatedPosition:
    """一次定位的结果 —— 全部字段都能回指真实文档。

    :param paragraph_index: 落在 ``ParseResult.paragraphs`` 里的段落序号（P6-2 契约）
    :param original_text: 该段落的**完整原文**（由本模块回填，模型不生成它）
    :param quote: 用于展示的证据片段；**一定能在** ``original_text`` 里找到
        （fallback 时二者相等，见 :func:`_fallback`）
    :param anchor_method: 这次定位是**怎么得到的**（见上面的两个常量）
    """

    paragraph_index: int
    original_text: str
    quote: str
    anchor_method: AnchorMethodValue


def locate_quote(
    *,
    paragraphs: list[Paragraph] | tuple[Paragraph, ...],
    clauses: list[Clause] | tuple[Clause, ...],
    clause_index: int,
    quote: str,
    context_before: str = "",
    context_after: str = "",
) -> LocatedPosition | None:
    """在 ``clause_index`` 指向的条款范围内定位 ``quote``。

    :param paragraphs: ``ParseResult.paragraphs`` —— 依赖 P6-2 的不变量：
        ``paragraphs[i].index == i``（保持文档顺序、从 0 连续递增），
        因此条款的 ``[start, end]`` 区间可以直接当切片下标用
    :param clauses: ``identify_clauses`` 的输出
    :param clause_index: **范围标签**（不是坐标）。找不到对应条款即视为不可用
    :param quote: 模型逐字复制的原文证据
    :param context_before: ``quote`` 紧邻的前文（可空）
    :param context_after: ``quote`` 紧邻的后文（可空）
    :return: 定位结果；**``None`` 表示这条 finding 不可用**（``clause_index`` 越界、
        或该条款里没有任何可锚定的段落）—— 调用方应当丢弃它，而不是自己猜一个位置。
        ⚠️ 注意区分：**fallback 不是 None** —— fallback 表示"风险保留，但只能定位到条款级"

    确认规则
    -------
    ::

        对每个含 quote 的段落：
            没有提供任何上下文 → 段落里有 quote 就算确认（出现几次不影响）
            提供了至少一段上下文 → 统计"满足全部给定上下文的 occurrence 数"：
                恰好 1 个 → 该段落被确认
                0 个 / ≥2 个 → 该段落不被确认
        恰好一个段落被确认 → CLAUSE_SCOPED；否则 → CLAUSE_FALLBACK

    **为什么无上下文时不看 occurrence 数**：本函数最终只定位到**段落**，
    同一段里同一句话出现两次，仍然只指向那一个段落 —— occurrence 不唯一
    并不等于 paragraph 不唯一。只有当我们**手里有额外的消歧信息**（上下文）时，
    才需要（也才应该）下钻到 occurrence 粒度：此时 0 个或 ≥2 个满足都意味着
    "指不出唯一的那一处"，按保守方向降级。

    ``occurrence_hint`` 为什么不在这里
    ------------------------------
    §10.5 明确它"仅作参考，**不作唯一依据**"。多个候选时用它去挑一个，就是在猜 ——
    与"宁可 fallback，不可错标"直接冲突。因此本函数的签名里**没有**这个参数：
    它留给调用方记日志，不参与定位决策。
    """
    clause = next((item for item in clauses if item.clause_index == clause_index), None)
    if clause is None:
        # 越界/无效的范围标签：模型报了一条本文档里不存在的条款 —— 直接丢弃
        logger.warning(
            "定位失败：clause_index=%s 不在本文档的条款里（共 %d 条），该 finding 被丢弃",
            clause_index,
            len(clauses),
        )
        return None

    window = _clause_paragraphs(paragraphs, clause)
    if not window:
        logger.warning("定位失败：clause_index=%s 的区间内没有段落，该 finding 被丢弃", clause_index)
        return None

    if not quote:
        # 没有证据就没有可核对的东西 —— 不猜，按已批准的 fallback 规则处理
        logger.warning("quote 为空：clause_index=%s 降级为条款级定位", clause_index)
        return _fallback(window, clause_index)

    candidates = [paragraph for paragraph in window if quote in paragraph.text]
    if not candidates:
        logger.warning("quote 在条款内找不到（不做模糊匹配）：clause_index=%s quote=%r", clause_index, quote)
        return _fallback(window, clause_index)

    # 对每个候选段落**逐个 occurrence** 确认（上下文只认紧邻，且必须同一个 occurrence 同时满足）
    confirmed = [
        paragraph
        for paragraph in candidates
        if _is_confirmed(paragraph, quote, context_before, context_after)
    ]
    if len(confirmed) == 1:
        return _scoped(confirmed[0], quote)

    # 一个都没确认（证据与上下文对不上），或确认出多个（谁也不能排除）——
    # **绝不挑一个最像的**
    logger.warning(
        "quote 在条款内出现于 %d 个段落，其中唯一确定的 %d 个，未能唯一确定：clause_index=%s quote=%r",
        len(candidates),
        len(confirmed),
        clause_index,
        quote,
    )
    return _fallback(window, clause_index)


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _clause_paragraphs(
    paragraphs: list[Paragraph] | tuple[Paragraph, ...], clause: Clause
) -> list[Paragraph]:
    """条款覆盖的段落（闭区间 ``[start_paragraph_index, end_paragraph_index]``，P7-1 契约）。

    直接用切片而不是切 ``Clause.text``：段落本身就是文档里的对象，
    没必要绕一圈再从文本拼回来（也就不会引入拼接/切分的二次解释）。
    """
    start = clause.start_paragraph_index
    end = clause.end_paragraph_index
    return [paragraph for paragraph in paragraphs if start <= paragraph.index <= end]


def _occurrences_in(text: str, quote: str) -> list[int]:
    """``quote`` 在 ``text`` 里的**全部**出现位置（允许重叠）。

    位置只是内部临时计算手段 —— **不进入 :class:`LocatedPosition` 契约**
    （本轮不引入字符偏移）。
    """
    positions: list[int] = []
    start = 0
    while (position := text.find(quote, start)) >= 0:
        positions.append(position)
        start = position + 1  # +1 而不是 +len：重叠出现也要算一次
    return positions


def _confirming_occurrences(text: str, quote: str, context_before: str, context_after: str) -> int:
    """``text`` 里**满足全部给定上下文**的 occurrence 个数（每个 occurrence 独立判定）。

    两个上下文都非空时，必须由**同一个** occurrence 同时满足两者 ——
    不允许"前文取一处、后文取另一处"拼出来（那等于人为造出一个并不存在的匹配）。

    判据是**确定的字面相邻**：前文以 ``context_before`` 结尾、后文以 ``context_after`` 开头。
    不用相似度、不设阈值 —— 打分会产生"最像的那个"，而"最像"不等于"正确"。

    上下文为空表示"不施加约束"；因此两个都为空时**每一个** occurrence 都满足。
    """
    count = 0
    for position in _occurrences_in(text, quote):
        before_ok = not context_before or text[:position].endswith(context_before)
        after_ok = not context_after or text[position + len(quote) :].startswith(context_after)
        if before_ok and after_ok:
            count += 1
    return count


def _has_context(context_before: str, context_after: str) -> bool:
    """是否提供了至少一段上下文 —— 它决定要不要进入 occurrence 级去歧义。"""
    return bool(context_before) or bool(context_after)


def _is_confirmed(paragraph: Paragraph, quote: str, context_before: str, context_after: str) -> bool:
    """这个段落的证据是否足以确认。

    **定位目标是段落，不是字符位置**，因此两种情形要分开处理：

    ==================================  ============================================
    没有任何上下文                       只要段落里有 ``quote`` 就算确认 ——
    （``context_before`` / ``after`` 全空） 出现 1 次还是 3 次都指向**同一个段落**
    提供了至少一段上下文                  进入 **occurrence 级去歧义**：恰好一个
                                          occurrence 同时满足全部上下文才算确认
    ==================================  ============================================

    后一种情形里，0 个（证据与上下文对不上）或 ≥2 个（段内多处都说得通）都不算确认 ——
    共同点是"指不出唯一的那一处"，方向一律保守：宁可降级到条款级，也不挑一个"最像的"。
    """
    if not _has_context(context_before, context_after):
        return quote in paragraph.text
    return _confirming_occurrences(paragraph.text, quote, context_before, context_after) == 1


def _scoped(paragraph: Paragraph, quote: str) -> LocatedPosition:
    """唯一确定：证据就在这一段的原文里。"""
    return LocatedPosition(
        paragraph_index=paragraph.index,
        original_text=paragraph.text,
        quote=quote,
        anchor_method=ANCHOR_CLAUSE_SCOPED,
    )


def _fallback(window: list[Paragraph], clause_index: int) -> LocatedPosition | None:
    """降级到条款级：锚在该条款的**首个有效段落**上。

    "有效"= 文本非空（P6-2 保留空段落，它们不能作为锚点）。
    ``quote`` 取该段落的原文 —— **模型给的那个 quote 在这里被丢弃**：
    它没能在文档里找到，把它当证据展示就是在展示一段无法核对的文本。
    （模型的说法仍然保留在 finding 的 ``reason`` 里，不会丢失。）

    :return: 整条条款都是空段落时返回 ``None``（没有任何可锚定的位置）
    """
    for paragraph in window:
        if paragraph.text.strip():
            logger.info(
                "降级为条款级定位：clause_index=%s → paragraph_index=%s（首个有效段落）",
                clause_index,
                paragraph.index,
            )
            return LocatedPosition(
                paragraph_index=paragraph.index,
                original_text=paragraph.text,
                quote=paragraph.text,
                anchor_method=ANCHOR_CLAUSE_FALLBACK,
            )
    return None


__all__ = [
    "ANCHOR_CLAUSE_FALLBACK",
    "ANCHOR_CLAUSE_SCOPED",
    "AnchorMethodValue",
    "LocatedPosition",
    "locate_quote",
]
