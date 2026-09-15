"""Clause Locator 纯函数测试（``understanding.locator.locate_quote``）。

每一处定位断言都同时回答两个问题：**落在哪一段**、**是怎么定到的**（anchor_method）。
只断言前者的话，"猜对了"和"定位对了"分不出来。

条款输入走真实的 P7-1 切分（``identify_clauses``），而不是手搓 ``Clause`` ——
这样 ``paragraph_index`` 的断言才真的验证了"定位能回到文档"。
"""

from __future__ import annotations

import ast
import inspect

from app.schemas.document import ParseResult
from app.schemas.understanding import Clause
from app.understanding.clauses import identify_clauses
from app.understanding.locator import (
    ANCHOR_CLAUSE_FALLBACK,
    ANCHOR_CLAUSE_SCOPED,
    locate_quote,
)
from tests.factories import make_parse_result, para


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _document(*texts: str) -> tuple[ParseResult, list[Clause]]:
    parsed = make_parse_result(*[para(text) for text in texts])
    return parsed, identify_clauses(parsed)


def _locate(parsed: ParseResult, clauses: list[Clause], **kwargs):
    kwargs.setdefault("paragraphs", parsed.paragraphs)
    kwargs.setdefault("clauses", clauses)
    return locate_quote(**kwargs)


#: 两条同类型条款，**引用**（第二条）里出现了与第一条完全相同的句子 ——
#: 用来验证"不得跨条款抢定位"
CROSS_CLAUSE_DOC = (
    "第一条 知识产权",
    "知识产权归乙方所有。",
    "第二条 违约责任",
    "双方均应遵守知识产权归乙方所有的约定。",
)


# --------------------------------------------------------------------------- #
# 唯一命中
# --------------------------------------------------------------------------- #
def test_unique_quote_locates_the_paragraph() -> None:
    parsed, clauses = _document(
        "第一条 知识产权",
        "本项目产生的知识产权归乙方所有。",
        "第二条 违约责任",
        "乙方应承担违约责任。",
    )

    located = _locate(parsed, clauses, clause_index=0, quote="知识产权归乙方")

    assert located is not None
    assert located.paragraph_index == 1
    assert located.anchor_method == ANCHOR_CLAUSE_SCOPED
    assert located.original_text == "本项目产生的知识产权归乙方所有。"
    assert located.quote == "知识产权归乙方"
    assert located.quote in located.original_text


def test_locates_within_a_multi_paragraph_clause() -> None:
    parsed, clauses = _document(
        "第一条 付款",
        "甲方应在签约后付款。",
        "预付款为合同总额的 30%。",
        "乙方开具发票。",
    )

    located = _locate(parsed, clauses, clause_index=0, quote="预付款为合同总额")

    assert located is not None
    assert located.paragraph_index == 2, "定位到条款内**第 3 段**，不是条款首段"
    assert located.original_text == "预付款为合同总额的 30%。"


# --------------------------------------------------------------------------- #
# 反向测试：不得跨条款抢定位
# --------------------------------------------------------------------------- #
def test_never_locates_outside_the_target_clause() -> None:
    """目标条款里**没有**这段原文，另一个条款里有 —— 必须 fallback，绝不能定到那边去。

    这是最容易出的错：全文搜一次就命中了，看起来"定位成功"，
    实际把 A 条款的风险标到了 B 条款上。
    """
    parsed, clauses = _document(*CROSS_CLAUSE_DOC)

    located = _locate(parsed, clauses, clause_index=0, quote="双方均应遵守")

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK
    assert located.paragraph_index == 0, "锚在**目标条款**的首个有效段落（第一条的编号段）"
    assert located.paragraph_index <= clauses[0].end_paragraph_index


def test_same_quote_in_two_clauses_picks_the_target_one() -> None:
    """同一个 quote 两条条款里都有：各定各的，互不串门。"""
    parsed, clauses = _document(*CROSS_CLAUSE_DOC)

    first = _locate(parsed, clauses, clause_index=0, quote="知识产权归乙方")
    second = _locate(parsed, clauses, clause_index=1, quote="知识产权归乙方")

    assert (first.paragraph_index, first.anchor_method) == (1, ANCHOR_CLAUSE_SCOPED)
    assert (second.paragraph_index, second.anchor_method) == (3, ANCHOR_CLAUSE_SCOPED)


# --------------------------------------------------------------------------- #
# 多次命中：上下文消歧
# --------------------------------------------------------------------------- #
MULTI_HIT_DOC = (
    "第一条 知识产权",
    "甲方不得主张知识产权归乙方所有。",
    "乙方也不得主张知识产权归乙方所有。",
)


def test_multiple_hits_without_context_fall_back() -> None:
    """同一条款内多处出现、又没有上下文 —— **绝不挑一个最像的**。"""
    parsed, clauses = _document(*MULTI_HIT_DOC)

    located = _locate(parsed, clauses, clause_index=0, quote="知识产权归乙方所有")

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK
    assert located.paragraph_index == 0


def test_context_before_disambiguates() -> None:
    parsed, clauses = _document(*MULTI_HIT_DOC)

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_before="乙方也不得主张",
    )

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_SCOPED
    assert located.paragraph_index == 2


def test_context_after_disambiguates() -> None:
    parsed, clauses = _document(
        "第一条 知识产权",
        "知识产权归乙方所有的约定无效，但另有约定的除外。",
        "知识产权归乙方所有的，甲方有权解除合同。",
    )

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_after="的约定无效",
    )

    assert located is not None
    assert located.paragraph_index == 1
    assert located.anchor_method == ANCHOR_CLAUSE_SCOPED


def test_both_contexts_must_match() -> None:
    """给两个上下文时**都要对上**才算确认 —— 只对一半不足以唯一。"""
    parsed, clauses = _document(*MULTI_HIT_DOC)

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_before="乙方也不得主张",
        context_after="完全不存在的后文",
    )

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK, "有一个对不上就不能伪造唯一结果"


def test_mismatching_context_never_fabricates_a_unique_result() -> None:
    """上下文与所有候选都不相符 → fallback，而不是"挑一个"。"""
    parsed, clauses = _document(*MULTI_HIT_DOC)

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_before="这句话在哪里都不存在",
        context_after="同样不存在",
    )

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK


def test_context_must_be_adjacent_not_merely_present() -> None:
    """上下文必须**紧邻** quote。

    两个候选段落里都**出现**了 ``context_before`` 的那串字，但都没有紧贴在 quote 前面
    （中间还隔着别的字）—— 因此谁也不能被确认，只能 fallback。
    如果实现退化成"这一段里出现过就算匹配"，这里就会**伪造出**一个唯一结果。
    """
    parsed, clauses = _document(
        "第一条 知识产权",
        "乙方也不得主张，无论如何知识产权归乙方所有。",
        "甲方不得主张，无论如何知识产权归乙方所有。",
    )

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_before="乙方也不得主张",  # 出现在段 1 里，但不紧邻 quote
    )

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK, "仅出现过不算确认"


def test_repeated_quote_in_one_paragraph_is_still_a_unique_paragraph() -> None:
    """同一段里出现两次、**且没有给出任何上下文** —— 仍是 ``CLAUSE_SCOPED``。

    **定位器的目标是 paragraph-level，occurrence 不唯一不影响 paragraph 唯一性**：
    两处都在同一段里，无论模型指的是哪一处，答案都是这一段。

    因此 occurrence 级去歧义**只在提供了上下文时才启动** ——
    手里没有额外的消歧信息却硬要按 occurrence 判唯一，只会把本来正确的
    段落定位降级掉（这正是本用例守住的边界）。
    """
    parsed, clauses = _document(
        "第一条 知识产权",
        "知识产权归乙方所有，且知识产权归乙方所有不可撤销。",
    )

    located = _locate(parsed, clauses, clause_index=0, quote="知识产权归乙方所有")

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_SCOPED
    assert located.paragraph_index == 1


# --------------------------------------------------------------------------- #
# 逐 occurrence 判定（P9-3 修正）：**给了上下文**时，同一段内多次出现要能认到**对的**那一次
#
# ⚠️ 与上一条的分界：只有提供了至少一段上下文，才进入 occurrence 级去歧义。
# --------------------------------------------------------------------------- #
SAME_PARAGRAPH_TWICE = (
    "第一条 知识产权",
    "甲方不得主张知识产权归乙方所有，但乙方也不得主张知识产权归乙方所有。",
)


def test_context_can_confirm_the_second_occurrence_in_a_paragraph() -> None:
    """第一次出现与上下文不符、**第二次**符合 —— 必须认第二次，给出 CLAUSE_SCOPED。

    这正是修正前的缺陷：只看 ``str.find()`` 的第一处，就会判成"对不上"而降级。
    """
    parsed, clauses = _document(*SAME_PARAGRAPH_TWICE)

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_before="但乙方也不得主张",
    )

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_SCOPED
    assert located.paragraph_index == 1


def test_context_after_can_confirm_the_second_occurrence() -> None:
    parsed, clauses = _document(
        "第一条 知识产权",
        "知识产权归乙方所有的情形之一，知识产权归乙方所有的情形之二。",
    )

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_after="的情形之二",
    )

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_SCOPED
    assert located.paragraph_index == 1


def test_two_confirming_occurrences_in_one_paragraph_fall_back() -> None:
    """两个 occurrence 都满足上下文 —— 仍然指不出唯一的那一处 → 降级。"""
    parsed, clauses = _document(
        "第一条 知识产权",
        "乙方也不得主张知识产权归乙方所有，丙方也不得主张知识产权归乙方所有。",
    )

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_before="也不得主张",  # 两处都紧邻以它结尾
    )

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK


def test_no_confirming_occurrence_in_a_paragraph_falls_back() -> None:
    """同一段里两次出现，但上下文与**哪一处**都对不上 → 降级（不是挑一个）。"""
    parsed, clauses = _document(
        "第一条 知识产权",
        "甲方不得主张知识产权归乙方所有，乙方也不得主张知识产权归乙方所有。",
    )

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_before="丙方也不得主张",  # 哪里都没有这串字
    )

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK


def test_both_contexts_must_belong_to_the_same_occurrence() -> None:
    """**两个上下文必须落在同一个 occurrence 上**。

    段 1 里：第一处的**前文**符合、第二处的**后文**符合 —— 但没有任何一处同时满足两者。
    如果实现允许"前文取一处、后文取另一处"，这里就会伪造出一个唯一结果。
    """
    parsed, clauses = _document(
        "第一条 知识产权",
        "甲方不得主张知识产权归乙方所有，乙方也不得主张知识产权归乙方所有的例外。",
    )

    located = _locate(
        parsed,
        clauses,
        clause_index=0,
        quote="知识产权归乙方所有",
        context_before="甲方不得主张",  # 只在**第一处**前面
        context_after="的例外",  # 只在**第二处**后面
    )

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK, "不许跨 occurrence 拼上下文"


# --------------------------------------------------------------------------- #
# fallback：找不到 / 空 quote / 空条款
# --------------------------------------------------------------------------- #
def test_quote_not_found_falls_back() -> None:
    parsed, clauses = _document("第一条 知识产权", "知识产权归甲方所有。")

    located = _locate(parsed, clauses, clause_index=0, quote="知识产权归乙方所有")

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK
    assert located.paragraph_index == 0
    assert located.quote == located.original_text, "fallback 时证据降级为回退段落的原文"


def test_similar_but_not_identical_quote_is_not_a_hit() -> None:
    """**不做模糊匹配**的行为证据：差一个字就是找不到，而不是"差不多"。
    （"知识产权归乙方"是段落的子串，但这里给的是**多了一个字**的变体。）"""
    parsed, clauses = _document("第一条 知识产权", "知识产权归乙方所有。")

    located = _locate(parsed, clauses, clause_index=0, quote="知识产权归属于乙方所有")

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK


def test_empty_quote_falls_back_without_guessing() -> None:
    parsed, clauses = _document("第一条 知识产权", "知识产权归乙方所有。")

    located = _locate(parsed, clauses, clause_index=0, quote="")

    assert located is not None
    assert located.anchor_method == ANCHOR_CLAUSE_FALLBACK
    assert located.paragraph_index == 0


def test_fallback_skips_empty_paragraphs() -> None:
    """空段落不能当锚点（P6-2 保留空段落）—— 锚到首个**有效**段落。"""
    parsed, clauses = _document("第一条 知识产权", "", "知识产权归甲方所有。")

    located = _locate(parsed, clauses, clause_index=0, quote="找不到的原文")

    assert located is not None
    assert located.paragraph_index == 0
    assert located.original_text.strip(), "锚点必须有实际内容"


def test_clause_with_only_empty_paragraphs_is_unusable() -> None:
    """整条条款都是空段落 —— 连 fallback 都没有可锚定的位置，返回 None（调用方丢弃）。"""
    parsed, clauses = _document("", "", "")

    assert len(clauses) == 1, "无编号文档整篇算一条条款（P7-1）"
    assert _locate(parsed, clauses, clause_index=0, quote="什么都找不到") is None


def test_document_without_paragraphs_has_no_clause_to_locate_in() -> None:
    """段落序列为空 → 没有任何条款（P7-1 的"无段落即无条款"）→ 无处可定。"""
    parsed = make_parse_result()
    clauses = identify_clauses(parsed)

    assert clauses == []
    assert _locate(parsed, clauses, clause_index=0, quote="x") is None


def test_clause_whose_text_is_blank_is_not_usable_as_an_anchor() -> None:
    """整篇只有一个空白段落 —— 没有任何有效段落可锚定，返回 None 而不是硬锚一个空段。"""
    parsed, clauses = _document("   ")

    assert len(clauses) == 1 and clauses[0].text == ""
    assert _locate(parsed, clauses, clause_index=0, quote="x") is None


def test_out_of_range_clause_index_is_unusable() -> None:
    parsed, clauses = _document("第一条 知识产权", "知识产权归乙方所有。")

    for bad_index in (1, 99, -1):
        assert _locate(parsed, clauses, clause_index=bad_index, quote="知识产权") is None


def test_out_of_range_clause_index_is_not_a_fallback() -> None:
    """越界是"这条不可用"（None），**不是** fallback —— 两者必须区分得开。"""
    parsed, clauses = _document("第一条 知识产权", "知识产权归乙方所有。")

    located = _locate(parsed, clauses, clause_index=99, quote="知识产权")

    assert located is None
    assert located is not ANCHOR_CLAUSE_FALLBACK


# --------------------------------------------------------------------------- #
# 定位结果必须来自真实文档
# --------------------------------------------------------------------------- #
def test_paragraph_index_points_at_a_real_paragraph() -> None:
    parsed, clauses = _document(*CROSS_CLAUSE_DOC)

    located = _locate(parsed, clauses, clause_index=1, quote="知识产权归乙方")

    assert located is not None
    paragraph = parsed.paragraphs[located.paragraph_index]
    assert paragraph.index == located.paragraph_index
    assert paragraph.text == located.original_text


def test_result_never_leaves_the_target_clause() -> None:
    parsed, clauses = _document(*CROSS_CLAUSE_DOC)

    for index in range(len(clauses)):
        located = _locate(parsed, clauses, clause_index=index, quote="知识产权")

        if located is None:
            continue
        assert clauses[index].start_paragraph_index <= located.paragraph_index
        assert located.paragraph_index <= clauses[index].end_paragraph_index


def test_document_is_never_modified() -> None:
    parsed, clauses = _document(*CROSS_CLAUSE_DOC)
    paragraphs_before = [p.model_copy() for p in parsed.paragraphs]
    clauses_before = [c.model_copy() for c in clauses]

    _locate(parsed, clauses, clause_index=1, quote="知识产权归乙方")

    assert parsed.paragraphs == paragraphs_before
    assert clauses == clauses_before


def test_locating_twice_is_deterministic() -> None:
    parsed, clauses = _document(*MULTI_HIT_DOC)
    kwargs = {"clause_index": 0, "quote": "知识产权归乙方所有"}

    assert _locate(parsed, clauses, **kwargs) == _locate(parsed, clauses, **kwargs)


# --------------------------------------------------------------------------- #
# 契约：本轮刻意不做的东西
# --------------------------------------------------------------------------- #
def test_locator_does_not_consume_occurrence_hint() -> None:
    """``occurrence_hint`` **不是**定位依据（§10.5：仅作参考，不作唯一依据）——
    多个候选时用它去挑一个就是在猜。因此签名里根本没有这个参数。"""
    parameters = set(inspect.signature(locate_quote).parameters)

    assert "occurrence_hint" not in parameters
    assert parameters == {"paragraphs", "clauses", "clause_index", "quote", "context_before", "context_after"}


def _imported_modules() -> set[str]:
    """模块**代码**里 import 的全部模块名。

    ⚠️ 用 AST 而不是扫源码文本：docstring 里会正当地提到"**不**实现 fuzzy / char offsets"
    这类词，按文本扫描会把说明文字当成违规（这条测试自己就踩过这个坑）。
    看 import 才是真的：**依赖集合**决定它能不能做到那些事。
    """
    import app.understanding.locator as locator_module

    tree = ast.parse(inspect.getsource(locator_module))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_locator_does_not_import_llm_types() -> None:
    """定位与 LLM 无关：本模块不认识 ``LLMFinding``，将来规则侧也能复用同一套定位。"""
    modules = _imported_modules()

    assert not any(module.startswith("app.llm") for module in modules)


def test_locator_dependency_set_is_pinned() -> None:
    """**依赖集合被钉死**：没有 difflib / numpy / 向量库 / fuzzy 库，也没有 app.llm。

    这比逐个扫关键字更强 —— 本轮明确不做的那些机制，靠的就是"根本没有可用的工具"。
    """
    assert _imported_modules() == {
        "__future__",
        "logging",
        "dataclasses",
        "typing",
        "app.schemas.document",
        "app.schemas.understanding",
    }
