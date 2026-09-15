"""风险合并（P9-9）。

本模块的每条用例都在回答同一个问题：**这两条风险凭什么被当成同一条？**
答案必须是一个确定性身份，而不是"看起来像"。因此这里**负面用例比正面用例多** ——
合并对了只是少一张卡片，合并错了是**不可逆地丢内容**。
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.core.constants import RiskSource
from app.risk.merge import merge_risk_items, normalize_risk_title
from app.risk.schemas import AgentRiskItem
from app.understanding.locator import ANCHOR_CLAUSE_FALLBACK, ANCHOR_CLAUSE_SCOPED

# --------------------------------------------------------------------------- #
# 夹具：直接构造统一风险项（不经 unify —— 这里测的是合并，不是映射）
# --------------------------------------------------------------------------- #
_PARAGRAPH_TEXT = "本项目产生的知识产权归乙方所有。"
_PARAGRAPH = 23


def _rule(**overrides) -> AgentRiskItem:
    payload = {
        "source": RiskSource.RULE,
        "risk_code": "IP_OWNER_SUPPLIER_001",
        "risk_title": "知识产权归属相对方",
        "dimension": "知识产权",
        "risk_level": "HIGH",
        "reason": "命中规则关键词：「知识产权归乙方」",
        "legal_basis": None,
        "original_text": _PARAGRAPH_TEXT,
        "quote": "知识产权归乙方",
        "paragraph_index": _PARAGRAPH,
        "anchor_method": None,
        "related_rule_code": None,
    }
    payload.update(overrides)
    return AgentRiskItem(**payload)


def _llm(**overrides) -> AgentRiskItem:
    payload = {
        "source": RiskSource.LLM,
        "risk_code": None,
        "risk_title": "知识产权归属供方",
        "dimension": "知识产权",
        "risk_level": "MEDIUM",
        "reason": "成果归属供方会限制我方后续使用。",
        "legal_basis": None,
        "original_text": _PARAGRAPH_TEXT,
        "quote": "知识产权归乙方所有",
        "paragraph_index": _PARAGRAPH,
        "anchor_method": ANCHOR_CLAUSE_SCOPED,
        "related_rule_code": None,
    }
    payload.update(overrides)
    return AgentRiskItem(**payload)


# --------------------------------------------------------------------------- #
# 15. 空输入
# --------------------------------------------------------------------------- #
def test_empty_input_yields_an_empty_list() -> None:
    assert merge_risk_items([]) == []


# --------------------------------------------------------------------------- #
# 1 / 2. 单独存在：原样保留，不被"凑对"
# --------------------------------------------------------------------------- #
def test_a_lone_rule_risk_is_returned_unchanged() -> None:
    rule = _rule()

    (merged,) = merge_risk_items([rule])

    assert merged is rule, "单项单元不复制也不改写"
    assert merged.source is RiskSource.RULE


def test_a_lone_llm_risk_is_returned_unchanged() -> None:
    llm = _llm()

    (merged,) = merge_risk_items([llm])

    assert merged is llm
    assert merged.source is RiskSource.LLM


# --------------------------------------------------------------------------- #
# 3 / 4. RULE ↔ RULE：同 risk_code 且同段
# --------------------------------------------------------------------------- #
def test_same_rule_code_at_the_same_paragraph_merges() -> None:
    items = [_rule(quote="知识产权归乙方"), _rule(quote="知识产权归乙方所有")]

    merged = merge_risk_items(items)

    assert len(merged) == 1
    assert merged[0].source is RiskSource.RULE, "同源合并**不**冒充跨源"


def test_different_rule_codes_at_the_same_paragraph_do_not_merge() -> None:
    """段落相同、维度相同、等级相同 —— 仍**不是**同一条风险：编码不同就是不同规则。"""
    items = [
        _rule(risk_code="IP_OWNER_SUPPLIER_001"),
        _rule(risk_code="IP_OWNER_SUPPLIER_002", risk_title="知识产权归属相对方（附条件）"),
    ]

    assert len(merge_risk_items(items)) == 2


def test_the_same_rule_code_at_different_paragraphs_does_not_merge() -> None:
    items = [_rule(paragraph_index=23), _rule(paragraph_index=24)]

    assert len(merge_risk_items(items)) == 2


# --------------------------------------------------------------------------- #
# 5 / 6 / 7. RULE ↔ LLM：related_rule_code 指向 + 同段
# --------------------------------------------------------------------------- #
def test_related_rule_code_at_the_same_paragraph_merges() -> None:
    items = [
        _rule(),
        _llm(related_rule_code="IP_OWNER_SUPPLIER_001"),
    ]

    merged = merge_risk_items(items)

    assert len(merged) == 1
    assert merged[0].source is RiskSource.RULE_AND_LLM


def test_related_rule_code_at_a_different_paragraph_does_not_merge() -> None:
    """关联编码对上了，**但不在同一段** —— 关联与证据位置必须同时成立。"""
    items = [
        _rule(paragraph_index=23),
        _llm(related_rule_code="IP_OWNER_SUPPLIER_001", paragraph_index=24),
    ]

    assert len(merge_risk_items(items)) == 2


def test_an_unmatched_related_rule_code_does_not_merge() -> None:
    """指向一条本批次里不存在的规则：不合并，**也不凭空造出那条规则**。"""
    items = [
        _rule(risk_code="IP_OWNER_SUPPLIER_001"),
        _llm(related_rule_code="LIAB_UNLIMITED_001"),
    ]

    merged = merge_risk_items(items)

    assert len(merged) == 2
    assert merged[1].source is RiskSource.LLM


def test_a_code_mismatch_does_not_merge_even_when_everything_else_matches() -> None:
    """**最关键的负面用例**：标题 / 理由 / dimension / quote / 段落**全都一样**，
    只是没有 ``related_rule_code`` 这一句声称 —— 依然不许合并。

    "长得像"不是身份。这条把关如果松了，整个合并层就退化成相似度匹配，
    而相似度匹配的误合并是不可逆的。
    """
    items = [
        _rule(),
        _llm(
            risk_title="知识产权归属相对方",  # 与规则标题逐字相同
            reason="命中规则关键词：「知识产权归乙方」",  # 与规则理由逐字相同
            quote="知识产权归乙方",  # 与规则 quote 逐字相同
            related_rule_code=None,  # ← 唯一缺少的：那句"我和规则 X 是同一件事"
        ),
    ]

    assert len(merge_risk_items(items)) == 2


# --------------------------------------------------------------------------- #
# 8 / 9 / 10. LLM ↔ LLM：规范化标题相同 + 同段
# --------------------------------------------------------------------------- #
def test_the_same_title_at_the_same_paragraph_merges() -> None:
    items = [_llm(), _llm(reason="重复报了一次。")]

    merged = merge_risk_items(items)

    assert len(merged) == 1
    assert merged[0].source is RiskSource.LLM, "两侧都是模型来源，结果仍是 LLM"


def test_the_same_title_at_different_paragraphs_does_not_merge() -> None:
    items = [_llm(paragraph_index=23), _llm(paragraph_index=24)]

    assert len(merge_risk_items(items)) == 2


def test_different_titles_at_the_same_paragraph_do_not_merge() -> None:
    items = [
        _llm(risk_title="知识产权归属供方"),
        _llm(risk_title="知识产权条款缺少期限"),
    ]

    assert len(merge_risk_items(items)) == 2


def test_whitespace_and_case_are_normalized_but_wording_is_not() -> None:
    """规范化只抹掉**不携带语义**的差异；少一个字、换一个词都不行。"""
    assert normalize_risk_title("知识产权归属供方") == normalize_risk_title(" 知识产权 归属供方 ")
    assert normalize_risk_title("IP Ownership") == normalize_risk_title("ip  ownership")
    assert normalize_risk_title("知识产权归属供方") != normalize_risk_title("知识产权归属相对方")

    assert len(merge_risk_items([_llm(), _llm(risk_title="知识产权 归属供方")])) == 1
    assert len(merge_risk_items([_llm(), _llm(risk_title="知识产权归属相对方")])) == 2


# --------------------------------------------------------------------------- #
# dimension：对不上就不合并（不挑、不猜）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "other",
    [
        _llm(related_rule_code="IP_OWNER_SUPPLIER_001", dimension="保密"),
        _llm(dimension="保密"),
    ],
    ids=["rule-vs-llm", "llm-vs-llm"],
)
def test_a_dimension_mismatch_blocks_the_merge(other: AgentRiskItem) -> None:
    """维度是"哪一类关注点"。对不上就说明两侧说的不是同一件事 ——
    **合并时需要挑一个维度出来的话，那就是猜**，所以直接不合并。
    """
    assert len(merge_risk_items([_rule(), other])) == 2


# --------------------------------------------------------------------------- #
# 11 / 12. 来源与等级
# --------------------------------------------------------------------------- #
def test_both_sources_are_kept_in_the_merged_item() -> None:
    merged = merge_risk_items([_rule(), _llm(related_rule_code="IP_OWNER_SUPPLIER_001")])

    assert merged[0].source is RiskSource.RULE_AND_LLM
    assert {m.value for m in RiskSource} >= {"RULE", "LLM", "RULE+LLM"}


@pytest.mark.parametrize(
    ("rule_level", "llm_level", "expected"),
    [
        ("LOW", "HIGH", "HIGH"),
        ("HIGH", "LOW", "HIGH"),
        ("MEDIUM", "LOW", "MEDIUM"),
        ("LOW", "MEDIUM", "MEDIUM"),
        ("MEDIUM", "HIGH", "HIGH"),
        ("HIGH", "MEDIUM", "HIGH"),
        ("LOW", "LOW", "LOW"),
        ("HIGH", "HIGH", "HIGH"),
    ],
)
def test_the_merged_level_is_the_higher_one(rule_level: str, llm_level: str, expected: str) -> None:
    items = [
        _rule(risk_level=rule_level),
        _llm(risk_level=llm_level, related_rule_code="IP_OWNER_SUPPLIER_001"),
    ]

    assert merge_risk_items(items)[0].risk_level == expected


def test_a_lone_risk_is_never_promoted() -> None:
    """**只有真正发生合并**时才取高 —— 一条孤立的 LOW 不会被"顺手升级"。"""
    assert merge_risk_items([_llm(risk_level="LOW")])[0].risk_level == "LOW"
    assert merge_risk_items([_rule(risk_level="LOW")])[0].risk_level == "LOW"


def test_an_unknown_level_is_not_compared() -> None:
    """词表外的等级出现在规则侧（规则目录可配任意字符串）—— 此时**不比较**。

    拿一个未知等级去和 HIGH/MEDIUM/LOW 比大小，比出来的任何结果都是编的；
    保留锚点自己的声明。
    """
    items = [
        _rule(risk_level="CRITICAL"),
        _llm(risk_level="HIGH", related_rule_code="IP_OWNER_SUPPLIER_001"),
    ]

    (merged,) = merge_risk_items(items)

    assert merged.risk_level == "CRITICAL", "不猜测 → 保留锚点值"


def test_an_unknown_level_on_the_other_side_is_not_compared() -> None:
    """反过来也一样：另一侧等级不在词表内时，仍然保留锚点值，不比较。"""
    items = [
        _rule(risk_level="LOW"),
        _llm(risk_level="URGENT", related_rule_code="IP_OWNER_SUPPLIER_001"),
    ]

    (merged,) = merge_risk_items(items)

    assert merged.risk_level == "LOW"


# --------------------------------------------------------------------------- #
# 13. 字段不被错误覆盖
# --------------------------------------------------------------------------- #
def test_the_anchor_wins_and_never_gets_overwritten() -> None:
    """锚点（规则）的非空字段一律保留；另一侧只在锚点**为空**时补位。"""
    rule = _rule(legal_basis="《民法典》第 843 条", reason="规则的命中理由")
    llm = _llm(
        related_rule_code="IP_OWNER_SUPPLIER_001",
        legal_basis="《著作权法》第 17 条",
        reason="模型的理由",
        risk_title="模型的标题",
    )

    (merged,) = merge_risk_items([rule, llm])

    assert merged.risk_title == "知识产权归属相对方", "标题取锚点"
    assert merged.reason == "规则的命中理由", "理由不许被另一侧覆盖"
    assert merged.legal_basis == "《民法典》第 843 条", "锚点有值就不补位"
    assert merged.quote == "知识产权归乙方", "证据不覆盖"
    assert merged.original_text == _PARAGRAPH_TEXT


def test_the_other_side_only_fills_the_anchor_s_gaps() -> None:
    """锚点**为空**的字段才由另一侧补进来 —— 空位补位，不是覆盖。"""
    rule = _rule(legal_basis=None)
    llm = _llm(related_rule_code="IP_OWNER_SUPPLIER_001", legal_basis="《著作权法》第 17 条")

    (merged,) = merge_risk_items([rule, llm])

    assert merged.legal_basis == "《著作权法》第 17 条", "规则没写法律依据 → 用模型的"


def test_the_rule_code_is_kept_and_the_association_is_preserved() -> None:
    """``risk_code`` 取规则的；``related_rule_code`` 是那句"关联声称"，保留下来。"""
    llm = _llm(related_rule_code="IP_OWNER_SUPPLIER_001")

    (merged,) = merge_risk_items([_rule(), llm])

    assert merged.risk_code == "IP_OWNER_SUPPLIER_001"
    assert merged.related_rule_code == "IP_OWNER_SUPPLIER_001"
    assert merged.anchor_method == ANCHOR_CLAUSE_SCOPED, "规则没有定位方式 → 用模型的"


@pytest.mark.parametrize("method", [ANCHOR_CLAUSE_SCOPED, ANCHOR_CLAUSE_FALLBACK])
def test_the_locator_signal_is_carried_over_untouched(method: str) -> None:
    """规则侧没有"定位方式"（它有确定的段落号），合并后由模型侧补进来。

    ``CLAUSE_FALLBACK`` 与 ``CLAUSE_SCOPED`` 的区别是**"这次定位可信吗"的信号** ——
    合并不许把它归一化掉，也不许给规则风险编一个。
    """
    llm = _llm(related_rule_code="IP_OWNER_SUPPLIER_001", anchor_method=method)

    (merged,) = merge_risk_items([_rule(), llm])

    assert merged.anchor_method == method
    assert merge_risk_items([_rule()])[0].anchor_method is None, "规则单独存在时仍然没有它"


def test_the_evidence_invariants_survive_the_merge() -> None:
    """合并出来的每一条仍然必须能指回原文。"""
    items = [
        _rule(),
        _llm(related_rule_code="IP_OWNER_SUPPLIER_001"),
        _rule(risk_code="IP_OWNER_SUPPLIER_001", quote="知识产权归乙方"),
    ]

    for merged in merge_risk_items(items):
        assert merged.quote in merged.original_text
        assert merged.quote != ""
        assert merged.paragraph_index >= 0


# --------------------------------------------------------------------------- #
# 锚点与顺序
# --------------------------------------------------------------------------- #
def test_the_rule_is_the_anchor_even_when_it_comes_last() -> None:
    """锚点是**规则**（确定的、有稳定编码），不因输入谁先谁后而改变字段口径。"""
    llm = _llm(related_rule_code="IP_OWNER_SUPPLIER_001", risk_title="模型的标题")
    rule = _rule(risk_title="知识产权归属相对方")

    (merged,) = merge_risk_items([llm, rule])

    assert merged.risk_title == "知识产权归属相对方"
    assert merged.risk_code == "IP_OWNER_SUPPLIER_001"
    assert merged.source is RiskSource.RULE_AND_LLM


def test_merge_is_order_independent() -> None:
    """同一组风险的字段结果不随输入顺序变化（顺序只影响输出位置，不影响内容）。"""
    rule = _rule()
    llm = _llm(related_rule_code="IP_OWNER_SUPPLIER_001")

    forward = merge_risk_items([rule, llm])
    backward = merge_risk_items([llm, rule])

    assert forward == backward


def test_merge_is_idempotent() -> None:
    """已经合并过的条目（``RULE+LLM``）原样透传 —— 重复合并不会把它再并一次。"""
    once = merge_risk_items([_rule(), _llm(related_rule_code="IP_OWNER_SUPPLIER_001")])

    twice = merge_risk_items(once)
    thrice = merge_risk_items([*once, _llm()])

    assert twice == once
    assert len(thrice) == 2, "已合并的条目不再吸收新的模型风险"


# --------------------------------------------------------------------------- #
# 16. 多风险场景：只合并合法 pair，不允许链式误合并
# --------------------------------------------------------------------------- #
def test_only_the_legitimate_pair_merges_in_a_crowded_scenario() -> None:
    """一段里挤了规则、重复的模型发现、无关的模型发现 —— 只有该并的并。

    期望：R1 + 两条同标题的模型发现 → 1 条；R2 独立；两条标题不同的模型发现
    （即使同段、同维度）各自独立。
    """
    items = [
        _rule(risk_code="IP_OWNER_SUPPLIER_001"),
        _rule(risk_code="LIAB_UNLIMITED_001", risk_title="责任上限缺失", dimension="违约责任"),
        _llm(related_rule_code="IP_OWNER_SUPPLIER_001"),
        _llm(related_rule_code="IP_OWNER_SUPPLIER_001"),
        _llm(risk_title="知识产权条款缺少期限"),
        _llm(risk_title="知识产权条款缺少地域范围"),
    ]

    merged = merge_risk_items(items)

    assert len(merged) == 4
    assert [m.source for m in merged] == [
        RiskSource.RULE_AND_LLM,
        RiskSource.RULE,
        RiskSource.LLM,
        RiskSource.LLM,
    ]


def test_a_risk_does_not_get_pulled_into_two_neighbouring_units() -> None:
    """两条规则、各有一条模型发现声称指向它 —— 两条关联各自成立，互不牵连。"""
    items = [
        _rule(risk_code="IP_OWNER_SUPPLIER_001", paragraph_index=23),
        _rule(risk_code="IP_OWNER_SUPPLIER_002", paragraph_index=24),
        _llm(related_rule_code="IP_OWNER_SUPPLIER_001", paragraph_index=23),
        _llm(related_rule_code="IP_OWNER_SUPPLIER_002", paragraph_index=24),
    ]

    merged = merge_risk_items(items)

    assert len(merged) == 2
    assert {m.risk_code for m in merged} == {"IP_OWNER_SUPPLIER_001", "IP_OWNER_SUPPLIER_002"}
    assert all(m.source is RiskSource.RULE_AND_LLM for m in merged)


# --------------------------------------------------------------------------- #
# 关联的多义：宁可各自保留，也不猜
# --------------------------------------------------------------------------- #
def test_a_rule_claimed_by_two_different_findings_is_not_associated() -> None:
    """两个**不同的**模型发现在同一段同时声称"我就是规则 R1" —— 这句声称自相矛盾。

    合并会出错（并进去的那条必然丢掉标题与理由），所以一条都不并：
    规则自己留着，两条模型发现各自留着。
    """
    items = [
        _rule(),
        _llm(risk_title="知识产权归属供方", related_rule_code="IP_OWNER_SUPPLIER_001"),
        _llm(risk_title="知识产权条款缺少期限", related_rule_code="IP_OWNER_SUPPLIER_001"),
    ]

    merged = merge_risk_items(items)

    assert len(merged) == 3
    assert merged[0].source is RiskSource.RULE
    assert [m.risk_title for m in merged] == [
        "知识产权归属相对方",
        "知识产权归属供方",
        "知识产权条款缺少期限",
    ]


def test_a_finding_claiming_two_rules_is_not_associated() -> None:
    """一条模型发现同时声称"和 R1、R2 都是同一件事" —— 同样是自相矛盾。

    （同标题同段的两条会先并成一个分组，分组里的关联声称因此可以有多条。）
    """
    items = [
        _rule(risk_code="IP_OWNER_SUPPLIER_001"),
        _rule(risk_code="IP_OWNER_SUPPLIER_002"),
        _llm(risk_title="同一个模型结论", related_rule_code="IP_OWNER_SUPPLIER_001"),
        _llm(risk_title="同一个模型结论", related_rule_code="IP_OWNER_SUPPLIER_002"),
    ]

    merged = merge_risk_items(items)

    assert len(merged) == 3, "两条规则 + 并成一条的模型发现"
    assert all(m.source is not RiskSource.RULE_AND_LLM for m in merged)


def test_an_ambiguous_finding_does_not_absorb_the_rule() -> None:
    """多义时**规则不被吸收**：它仍然是一条独立的风险，不是被吞掉。"""
    items = [
        _rule(risk_level="HIGH"),
        _llm(risk_title="A", related_rule_code="IP_OWNER_SUPPLIER_001"),
        _llm(risk_title="B", related_rule_code="IP_OWNER_SUPPLIER_001"),
    ]

    merged = merge_risk_items(items)

    assert [m.risk_title for m in merged] == ["知识产权归属相对方", "A", "B"]


# --------------------------------------------------------------------------- #
# 14 / 纯函数性质
# --------------------------------------------------------------------------- #
def test_inputs_are_not_mutated() -> None:
    """合并**只读不写** —— 输入对象在合并前后逐字段一致。"""
    items = [
        _rule(),
        _llm(related_rule_code="IP_OWNER_SUPPLIER_001", legal_basis="《著作权法》第 17 条"),
        _rule(risk_code="LIAB_UNLIMITED_001", dimension="违约责任"),
    ]
    before = [item.model_dump() for item in items]

    merge_risk_items(items)

    assert [item.model_dump() for item in items] == before


def test_the_merged_item_is_a_new_object_but_a_lone_one_is_shared() -> None:
    """多项单元产出**新对象**（改它不会波及输入）；单项单元原样返回。"""
    rule = _rule()
    llm = _llm(related_rule_code="IP_OWNER_SUPPLIER_001")

    (merged,) = merge_risk_items([rule, llm])
    (lone,) = merge_risk_items([_llm(risk_title="单独一条")])

    assert merged is not rule and merged is not llm
    assert lone is not rule


def test_merging_is_deterministic() -> None:
    def run() -> list[AgentRiskItem]:
        return merge_risk_items(
            [
                _rule(),
                _llm(related_rule_code="IP_OWNER_SUPPLIER_001"),
                _rule(risk_code="LIAB_UNLIMITED_001", dimension="违约责任", paragraph_index=30),
            ]
        )

    assert run() == run()


def test_merge_never_logs(caplog: pytest.LogCaptureFixture) -> None:
    """合并层**不记日志**：领域层保持纯粹，所有"放弃"分支都是静默的。

    输入刻意把所有放弃分支都踩一遍：多义关联（A / B 同时声称规则 R1）、
    词表外的等级、以及不满足任何合并条件的独立条目。
    """
    items = [
        _rule(risk_level="CRITICAL"),
        _llm(risk_title="A", related_rule_code="IP_OWNER_SUPPLIER_001"),
        _llm(risk_title="B", related_rule_code="IP_OWNER_SUPPLIER_001"),
        _llm(risk_title="谁也不关联的一条"),
    ]

    with caplog.at_level("DEBUG"):
        merged = merge_risk_items(items)

    assert len(merged) == 4, "行为不变：该不并的仍然不并"
    assert caplog.records == [], "合并层不产生任何日志记录"


def test_merge_depends_on_nothing_but_the_domain() -> None:
    """依赖集合被钉死：不做 IO、**不 logging**、不碰 Graph/State/DB、不认识 HTTP。

    没有 ``logging`` 这一项不是巧合 —— 它是这条断言要守住的东西：
    领域层一旦开始记日志，"纯函数"这个性质就名不副实了。
    """
    import app.risk.merge as module

    tree = ast.parse(inspect.getsource(module))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert modules == {
        "__future__",
        "app.core.constants",
        "app.risk.schemas",
        "collections.abc",
    }
    assert not hasattr(module, "logger")
