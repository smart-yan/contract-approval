"""元数据抽取纯函数（``understanding.metadata.extract_metadata``）。

只测抽取规则本身：输入是段落序列，输出是 ``MetadataItem`` 列表。
真实合同的端到端验收在 ``tests/integration/test_golden_sample_metadata.py``。
"""

from __future__ import annotations

import pytest

from app.core.constants import ExtractMethod
from app.understanding.metadata import extract_metadata
from tests.factories import make_parse_result, para, row


def _by_key(items, key: str):
    return next((i for i in items if i.field_key == key), None)


def _value(items, key: str) -> str | None:
    item = _by_key(items, key)
    return item.field_value if item else None


# --------------------------------------------------------------------------- #
# 正常抽取
# --------------------------------------------------------------------------- #
def test_extracts_parties_credit_code_amount_and_dates() -> None:
    result = make_parse_result(
        para("软件采购合同"),
        para("甲方（采购方）：启明数智科技（上海）有限公司"),
        para("统一社会信用代码：91310115MA1K3QXW2P"),
        para("乙方（供应方）：远航软件技术有限公司"),
        para("1.2 合同总金额为人民币壹佰贰拾万元整（¥1,200,000.00），含税。"),
        para("本合同自双方盖章之日起生效，有效期至 2029 年 9 月 13 日止。"),
        para("签订日期：2026 年 9 月 14 日"),
    )

    items = extract_metadata(result)

    assert _value(items, "our_party_name") == "启明数智科技（上海）有限公司"
    assert _value(items, "counterparty_name") == "远航软件技术有限公司"
    assert _value(items, "credit_code") == "91310115MA1K3QXW2P"
    assert _value(items, "contract_amount") == "1200000.00"
    assert _value(items, "currency") == "CNY"
    assert _value(items, "expire_date") == "2029-09-13"
    assert _value(items, "sign_date") == "2026-09-14"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("合同金额为 ¥1,234,567.89", "1234567.89"),
        ("合同金额为人民币 5000 元", "5000.00"),
        ("合同金额为 1200 元", "1200.00"),
        ("合同金额为￥99.5", "99.50"),
    ],
)
def test_amount_forms_are_normalized(text: str, expected: str) -> None:
    """金额一律规范成两位小数的纯数字串 —— 下游拿到就能直接转 Decimal。"""
    items = extract_metadata(make_parse_result(para(text)))

    assert _value(items, "contract_amount") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("甲方：某某有限公司", "某某有限公司"),
        ("甲方（采购方）：某某有限公司", "某某有限公司"),
        ("甲方(采购方):某某有限公司", "某某有限公司"),
        ("乙方：某某有限公司", None),  # 乙方不产出 our_party_name
    ],
)
def test_party_name_forms(text: str, expected: str | None) -> None:
    items = extract_metadata(make_parse_result(para(text)))

    assert _value(items, "our_party_name") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("签订日期：2026 年 9 月 14 日", "2026-09-14"),
        ("签署日期：2026-09-14", "2026-09-14"),
        ("签约日期：2026/9/4", "2026-09-04"),
    ],
)
def test_date_forms_are_normalized_to_iso(text: str, expected: str) -> None:
    items = extract_metadata(make_parse_result(para(text)))

    assert _value(items, "sign_date") == expected


# --------------------------------------------------------------------------- #
# 字段不存在
# --------------------------------------------------------------------------- #
def test_empty_document_yields_nothing() -> None:
    assert extract_metadata(make_parse_result()) == []


def test_document_without_any_anchor_yields_nothing() -> None:
    result = make_parse_result(para("这份文档里什么元数据都没有。"), para("只是普通正文。"))

    assert extract_metadata(result) == []


def test_missing_field_is_simply_absent() -> None:
    """抽不到就不产出该条，而不是产出一条空值。"""
    result = make_parse_result(para("甲方：某某有限公司"), para("正文"))

    items = extract_metadata(result)

    assert _value(items, "our_party_name") == "某某有限公司"
    assert _by_key(items, "credit_code") is None
    assert _by_key(items, "sign_date") is None


def test_effective_date_absent_when_label_has_no_adjacent_date() -> None:
    """「自双方盖章之日起生效」没有日期 ⇒ 不产出 effective_date。

    ⚠️ 同一段里还有「有效期至 2029 年 9 月 13 日」。如果实现退化成"在这一段里
    找日期"，生效日就会被错填成到期日 —— 宁可抽不到，也不能填错。
    """
    result = make_parse_result(para("本合同自双方盖章之日起生效，有效期至 2029 年 9 月 13 日止。"))

    items = extract_metadata(result)

    assert _by_key(items, "effective_date") is None
    assert _value(items, "expire_date") == "2029-09-13"


def test_effective_date_extracted_when_date_is_adjacent() -> None:
    result = make_parse_result(para("本合同自 2026 年 10 月 1 日起生效。"))

    assert _value(extract_metadata(result), "effective_date") == "2026-10-01"


# --------------------------------------------------------------------------- #
# paragraph_index 与 quote
# --------------------------------------------------------------------------- #
def test_paragraph_index_points_at_the_source_paragraph() -> None:
    result = make_parse_result(
        para("标题"),
        para("甲方：某某有限公司"),
        para("正文"),
        para("签订日期：2026 年 9 月 14 日"),
    )

    items = extract_metadata(result)

    assert _by_key(items, "our_party_name").paragraph_index == 1
    assert _by_key(items, "sign_date").paragraph_index == 3


def test_quote_is_the_matched_text() -> None:
    result = make_parse_result(para("甲方（采购方）：某某有限公司"))

    item = _by_key(extract_metadata(result), "our_party_name")

    assert item.quote == "甲方（采购方）：某某有限公司"


def test_amount_quote_keeps_the_original_form() -> None:
    """值规范化了，但 quote 保留原文 —— 人工核对时看的是原话。"""
    result = make_parse_result(para("合同金额为 ¥1,200,000.00"))

    item = _by_key(extract_metadata(result), "contract_amount")

    assert item.field_value == "1200000.00"
    assert item.quote == "¥1,200,000.00"


# --------------------------------------------------------------------------- #
# 多候选值：按文档顺序取第一个
# --------------------------------------------------------------------------- #
def test_first_party_declaration_wins_over_the_signature_block() -> None:
    result = make_parse_result(
        para("甲方（采购方）：启明数智科技（上海）有限公司"),
        para("第一条 合同标的"),
        para("甲方（盖章）：启明数智科技（上海）有限公司"),
        para("签订日期：2026 年 9 月 14 日"),
    )

    items = extract_metadata(result)

    assert _by_key(items, "our_party_name").paragraph_index == 0, "取文档里第一个主体声明"
    assert _by_key(items, "sign_date").paragraph_index == 3


def test_first_credit_code_wins() -> None:
    result = make_parse_result(
        para("统一社会信用代码：91310115MA1K3QXW2P"),
        para("统一社会信用代码：91110108MA01YB7T3D"),
    )

    assert _value(extract_metadata(result), "credit_code") == "91310115MA1K3QXW2P"


def test_first_sign_date_wins_when_signed_twice() -> None:
    result = make_parse_result(
        para("签订日期：2026 年 9 月 14 日"),
        para("甲方（盖章）：某某"),
        para("签订日期：2026 年 9 月 15 日"),
    )

    assert _value(extract_metadata(result), "sign_date") == "2026-09-14"


def test_selection_is_stable_across_runs() -> None:
    result = make_parse_result(
        para("甲方：A 公司"),
        para("甲方：B 公司"),
        para("签订日期：2026 年 1 月 1 日"),
        para("签订日期：2026 年 2 月 2 日"),
    )

    first = extract_metadata(result)
    second = extract_metadata(result)

    assert [i.model_dump() for i in first] == [i.model_dump() for i in second]


# --------------------------------------------------------------------------- #
# 不误识别
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "乙方应于收到货物后 30 日内支付全部货款。",
        "甲方应在 15 个工作日内完成验收。",
        "本合同一式肆份，甲乙双方各执贰份。",
        "有效期至 2029 年 9 月 13 日止。",
    ],
)
def test_plain_numbers_are_not_mistaken_for_amounts(text: str) -> None:
    """正文里的普通数字（天数 / 份数 / 年份）不能变成合同金额。"""
    items = extract_metadata(make_parse_result(para(text)))

    assert _by_key(items, "contract_amount") is None


def test_bare_eighteen_digit_number_is_not_a_credit_code() -> None:
    """信用代码必须带标签 —— 18 位纯数字在正文里并不罕见。"""
    result = make_parse_result(para("合同流水号：913101152026091412"))

    assert _by_key(extract_metadata(result), "credit_code") is None


def test_long_sentence_starting_with_party_is_not_a_party_name() -> None:
    """标签与冒号之间限长，挡住「甲方应当在收到货物后 30 日内：…」这类正文句。"""
    result = make_parse_result(para("甲方应于收到全部货物并完成验收后的三十个自然日内：支付款项。"))

    assert _by_key(extract_metadata(result), "our_party_name") is None


# --------------------------------------------------------------------------- #
# 币种
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("合同金额为人民币 100 元", "CNY"),
        ("合同金额为 ¥100", "CNY"),
        ("合同金额为 100 元", "CNY"),
        ("合同金额为 100 美元", "USD"),
        ("合同金额为 $100", "USD"),
        ("合同金额为 100 欧元", "EUR"),
    ],
)
def test_currency_detection(text: str, expected: str) -> None:
    """「美元」含「元」—— 词表顺序反了会全部判成 CNY。"""
    items = extract_metadata(make_parse_result(para(text)))

    assert _value(items, "currency") == expected


def test_currency_comes_from_the_amount_paragraph() -> None:
    """币种只在金额所在段落里找，不从别处错配。"""
    result = make_parse_result(
        para("附件报价以美元计。"),
        para("合同金额为 ¥1,000.00"),
    )

    assert _value(extract_metadata(result), "currency") == "CNY"


# --------------------------------------------------------------------------- #
# 付款条件
# --------------------------------------------------------------------------- #
def test_payment_terms_takes_the_whole_table_block() -> None:
    result = make_parse_result(
        para("第三条 付款方式"),
        para("付款计划如下："),
        row("付款阶段", "比例", "付款条件"),
        row("1. 预付款", "30%", "合同生效后支付"),
        row("2. 到货款", "60%", "交付后支付"),
        para("以上为全部付款安排。"),
    )

    item = _by_key(extract_metadata(result), "payment_terms")

    assert item.paragraph_index == 2, "从表格块的第一行开始"
    assert item.field_value.split("\n") == [
        "付款阶段\t比例\t付款条件",
        "1. 预付款\t30%\t合同生效后支付",
        "2. 到货款\t60%\t交付后支付",
    ]


def test_payment_terms_falls_back_to_a_paragraph() -> None:
    result = make_parse_result(para("甲方应按合同总额的 30% 支付预付款。"))

    item = _by_key(extract_metadata(result), "payment_terms")

    assert item.field_value == "甲方应按合同总额的 30% 支付预付款。"
    assert item.paragraph_index == 0


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #
def test_extract_method_is_regex_for_every_item() -> None:
    result = make_parse_result(
        para("甲方：某某有限公司"),
        para("合同金额为 ¥100.00"),
    )

    items = extract_metadata(result)

    assert items
    assert {i.extract_method for i in items} == {ExtractMethod.REGEX.value}


def test_value_types_are_assigned_per_field() -> None:
    result = make_parse_result(
        para("甲方：某某有限公司"),
        para("统一社会信用代码：91310115MA1K3QXW2P"),
        para("合同金额为 ¥100.00"),
        para("签订日期：2026 年 9 月 14 日"),
    )

    types = {i.field_key: i.value_type for i in extract_metadata(result)}

    assert types["our_party_name"] == "TEXT"
    assert types["credit_code"] == "CODE"
    assert types["contract_amount"] == "AMOUNT"
    assert types["sign_date"] == "DATE"


def test_items_follow_the_catalog_order() -> None:
    result = make_parse_result(
        para("签订日期：2026 年 9 月 14 日"),
        para("甲方：某某有限公司"),
    )

    keys = [i.field_key for i in extract_metadata(result)]

    assert keys == ["our_party_name", "sign_date"], "输出顺序按字段目录，不按命中顺序"
