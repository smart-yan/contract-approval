"""条款切分（P7-1）。

职责边界
-------
::

    ParseResult  →  list[Clause]

**只做切分**：把段落序列按编号切成条款，并给出每一条的类型标签。
不认识风险、不抽元数据、不取关键词、不调 LLM —— 那些是别的能力域的事。

两件事都必须**可解释、确定性、局部化**
-----------------------------------
1. **边界识别**只做正则匹配（下面三张模式表）
2. **类型判定**只扫描条款的**编号段那一个 Paragraph**，不扫描条款全文

第 2 条是刻意的：拿整个 ``Clause.text`` 去做关键词分类会引入"某个词出现在正文里
就算这个条款是那种类型"的误判，而且让判定依赖于切分结果 —— 两件事互相纠缠后
就没法单独解释"为什么这条被判成 IP"。只看标题行，判定范围是**局部**的。

``title`` 同样**只从编号段派生**：去掉编号后剩下的非空文字就是标题，
与 ``clause_type`` 用的是同一行输入。它**不取决于条款跨几段** ——
"是否跨多段"和"编号段里有没有标题"是两个不同维度（详见 ``_build_clause``）。

编号模式与优先级
--------------
================  ==============================  ==========  ==============================
模式               正则                             最低次数     说明
================  ==============================  ==========  ==============================
``TIAO``          ``第[数字]+条``（允许空格）          **1**      无歧义：段落以它开头就是条款标题
``CN_NUM``        ``一、``                           2        与列表项同形，需要多次出现才认
``ARAB``          ``1.`` / ``1、``（见下）            2        同上
================  ==============================  ==========  ==============================

**为什么 TIAO 只要 1 次**：一份补充协议可能只有一个「第一条」。``第X条`` 是强模式，
一个就足以说明这份文档用条款编号组织；而 ``一、`` / ``1.`` 与普通列表项同形，
必须靠"多次出现"来区分。

**``ARAB`` 的 ``(?!\\d)`` 是必须的**：没有它，``1.1`` / ``1.2`` / ``2.1`` 全部会被
当成条款起点 —— 在一份八条、每条第 2~3 款的合同上，会把 8 个条款切成 18 个。
"""

from __future__ import annotations

import re

from app.core.constants import ClauseType, ExtractMethod
from app.schemas.document import Paragraph, ParseResult
from app.schemas.understanding import Clause

# --------------------------------------------------------------------------- #
# 编号模式
# --------------------------------------------------------------------------- #
#: 「第X条」：支持「第一条」「第1条」「第 1 条」（编号两侧允许空白）。
#: 刻意**不**扩展到「第（一）条」「1.1」—— 那些是层级编号，不是条款边界。
_TIAO = re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*条")

#: 「一、」：只认顿号。合同里几乎不写「一.」，而放开点号会引入大量噪声。
_CN_NUM = re.compile(r"^[一二三四五六七八九十]+、")

#: 「1.」「1、」：``(?!\d)`` 保证「1.1」不会被当成条款起点。
_ARAB = re.compile(r"^\d+[.、](?!\d)")

#: 模式探测顺序 = 优先级；(模式名, 正则, 启用所需最低命中次数)
_NUMBERING_MODES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("TIAO", _TIAO, 1),
    ("CN_NUM", _CN_NUM, 2),
    ("ARAB", _ARAB, 2),
)

# --------------------------------------------------------------------------- #
# 编号段 → 条款类型（受控标题关键词表）
# --------------------------------------------------------------------------- #
#: 按**标题词的独特程度**排序：越独特的类型越靠前。
#:
#: * 专题性条款（不可抗力 / 知识产权 / 保密 / 数据安全 / 争议）标题词最独特
#: * 义务性条款（违约 / 验收 / 交付）居中
#: * 基础性条款（主体 / 付款）标题词最泛 —— "金额""标的"可能出现在任何标题里，
#:   放最后，避免它们抢走本该属于专题性条款的分类
#:
#: 命中即返回，因此这张表的顺序**就是**优先级；调整顺序等于调整分类结果。
_TYPE_KEYWORDS: tuple[tuple[ClauseType, tuple[str, ...]], ...] = (
    (ClauseType.FORCE_MAJEURE, ("不可抗力",)),
    (ClauseType.IP, ("知识产权", "著作权", "专利", "商标", "源代码")),
    (ClauseType.CONFIDENTIAL, ("保密", "商业秘密", "机密")),
    (ClauseType.DATA_SECURITY, ("数据安全", "个人信息", "网络安全", "数据保护")),
    (ClauseType.DISPUTE, ("争议", "仲裁", "诉讼", "管辖")),
    (ClauseType.LIABILITY, ("违约", "赔偿", "责任")),
    (ClauseType.ACCEPTANCE, ("验收", "检验", "测试")),
    (ClauseType.DELIVERY, ("交付", "交货", "供货", "实施")),
    (ClauseType.SUBJECT, ("主体", "资格", "资质", "当事人", "标的")),
    (ClauseType.AMOUNT_PAYMENT, ("付款", "支付", "价款", "金额", "结算", "发票")),
)


def classify_clause_type(heading: str) -> ClauseType:
    """按编号段的标题文字判定条款类型；无命中返回 :attr:`ClauseType.OTHER`。

    **只看这一行**，不看条款正文 —— 见模块 docstring 的理由。
    """
    for clause_type, keywords in _TYPE_KEYWORDS:
        if any(keyword in heading for keyword in keywords):
            return clause_type
    return ClauseType.OTHER


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def identify_clauses(parse_result: ParseResult) -> list[Clause]:
    """把 ``ParseResult.paragraphs`` 切成条款。

    **不抛异常** —— 任何输入都返回一个可用的列表（最差情况是"整篇一个条款"）。

    不变量：返回的条款区间构成 ``[0, len(paragraphs)-1]`` 的**完整划分**
    （无重叠、无遗漏）。段落为空时返回空列表。
    """
    paragraphs = parse_result.paragraphs
    if not paragraphs:
        return []

    starts = _find_clause_starts(paragraphs)

    if not starts:
        # 没有任何编号模式启用 ⇒ 整篇作为一个条款，**绝不返回空列表**
        # （返回空会让下游以为"这份合同没有内容可审"，丢的是整份合同）
        return [_build_clause(paragraphs, 0, len(paragraphs) - 1, clause_index=0, marker=None)]

    clauses: list[Clause] = []

    # 第一个编号段之前的内容 → 前言条款（clause_no=None）
    if starts[0][0] > 0:
        clauses.append(_build_clause(paragraphs, 0, starts[0][0] - 1, clause_index=0, marker=None))

    bounds = [index for index, _ in starts] + [len(paragraphs)]
    for i, (start, match) in enumerate(starts):
        end = bounds[i + 1] - 1
        clauses.append(
            _build_clause(
                paragraphs,
                start,
                end,
                clause_index=len(clauses),
                marker=match,
            )
        )
    return clauses


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _find_clause_starts(paragraphs: list[Paragraph]) -> list[tuple[int, re.Match[str]]]:
    """探测主编号模式，返回 ``[(段落序号, 匹配对象), ...]``。

    只考察 ``block_type == "PARAGRAPH"`` 的段落 —— **表格行永远不能开启新条款**。
    合同表格里常出现「1. 预付款」这类文本，放进来的话会被当成条款起点。
    """
    mode = _detect_numbering_mode(paragraphs)
    if mode is None:
        return []

    pattern = next(rx for name, rx, _ in _NUMBERING_MODES if name == mode)

    starts: list[tuple[int, re.Match[str]]] = []
    for paragraph in paragraphs:
        if paragraph.block_type != "PARAGRAPH":
            continue
        match = pattern.match(paragraph.text)
        if match is not None:
            starts.append((paragraph.index, match))
    return starts


def _detect_numbering_mode(paragraphs: list[Paragraph]) -> str | None:
    """按优先级选主编号模式；没有模式达到最低次数则返回 ``None``。"""
    for name, pattern, min_hits in _NUMBERING_MODES:
        hits = sum(1 for p in paragraphs if p.block_type == "PARAGRAPH" and pattern.match(p.text) is not None)
        if hits >= min_hits:
            return name
    return None


def _build_clause(
    paragraphs: list[Paragraph],
    start: int,
    end: int,
    *,
    clause_index: int,
    marker: re.Match[str] | None,
) -> Clause:
    """按闭区间 ``[start, end]`` 组装一个条款。

    ``marker`` 为 ``None`` 表示这不是由编号段开启的条款（前言 / 无编号文档）。
    """
    text = "\n".join(p.text for p in paragraphs[start : end + 1])

    if marker is None:
        return Clause(
            clause_index=clause_index,
            clause_no=None,
            title=None,
            clause_type=ClauseType.OTHER.value,
            start_paragraph_index=start,
            end_paragraph_index=end,
            text=text,
            extract_method=ExtractMethod.RULE.value,
        )

    heading = paragraphs[start].text
    clause_no = marker.group(0)
    # 标题**只从编号段来**：去掉编号后剩下的非空文字就是标题。
    #
    # ⚠️ 它**不取决于条款跨几段**。"是否跨多段"与"编号段里有没有标题"是两个不同
    # 维度 —— 用前者去推断后者，会让「第一条 合同标的」这种单段条款丢掉标题，
    # 而它的标题明明就写在编号后面。
    #
    # 也**不**去后续正文段落里找标题：那需要启发式（"下一段短不短""有没有冒号"），
    # 会引入第二套判断标准，且与"判定必须局部化"冲突。
    title = heading[marker.end() :].strip() or None

    return Clause(
        clause_index=clause_index,
        clause_no=clause_no,
        title=title,
        clause_type=classify_clause_type(heading).value,
        start_paragraph_index=start,
        end_paragraph_index=end,
        text=text,
        extract_method=ExtractMethod.RULE.value,
    )


__all__ = ["classify_clause_type", "identify_clauses"]
