"""元数据抽取（P7-2）。

职责边界
-------
::

    ParseResult  →  list[MetadataItem]

**只从段落文本里读事实**，全部是确定性规则（正则 + 少量结构化判断），不调 LLM。

不抽什么（与 Backend 的分工）
--------------------------
``contract_no`` / ``title`` / ``contract_type`` / ``dept`` 这些是**用户在上传时声明的
业务事实**，Backend 的主数据里已经有。从文档里再猜一遍有两个后果：一是两个真相源，
二是**猜错会静默改变规则集的选择**（``contract_type`` 决定用哪套规则）。因此坚决不抽。

同理，**也不回写** ``contract`` 表 —— Agent 改 Backend 的主数据是越界。
文档里读到的值进 ``contract_metadata``（带出处、可人工校对），
与用户声明的值并列展示：**两者不一致本身就是有价值的信号**。

多候选值怎么办
------------
统一规则：**按文档顺序取第一个命中**（段落序号小的优先；同段内取最早出现的）。
它足够确定、可解释，也不需要额外的置信度打分。

每个字段的抽取规则
----------------
======================  ==========================================================
字段                     规则
======================  ==========================================================
``our_party_name``      ``甲方…：<名称>``，段落开头锚定，括号注（如「（采购方）」）允许
``counterparty_name``   同上，``乙方``
``credit_code``         ``统一社会信用代码：<18 位>``，**必须带标签**
``contract_amount``     ``¥/￥<数字>``、``人民币<数字>元``、``<数字>元``，规范化为两位小数
``currency``            在**金额所在段落**里找币种词（美元优先于「元」，见 ``_CURRENCIES``）
``sign_date``           ``签订/签署/签约日期：<日期>``
``effective_date``      ``生效日期：<日期>`` 或 ``自<日期>起生效``（日期必须**紧邻**标签）
``expire_date``         ``有效期至<日期>`` / ``到期日：<日期>``
``payment_terms``       第一处含 ``%`` / ``百分之`` 的**内容块**（表格块或单段落）
``prepay_ratio``        **表格行**里「标签含预付款 + 同行有百分比」的第一处，规范化为比例
======================  ==========================================================

两条刻意的保守选择
----------------
1. **日期必须紧邻标签**。合同里同一段常同时出现"起生效"和"有效期至 2029 年 9 月 13 日"
   （黄金样例的第 35 段就是），若退化成"在这一段里找日期"，生效日会被错填成到期日。
   宁可抽不到，也不能填错 —— 这是法务场景，错值比缺值危险。
2. **信用代码必须带标签**。18 位纯数字在正文里并不罕见（编号、流水号），
   不锚定标签就有误识别风险。
3. **``prepay_ratio`` 只认表格行**。付款计划表里「预付款 | 30%」是**结构化事实**：
   标签与比例在同一行的固定位置上，读出来不需要理解语义。而「预付三成」
   「预付一部分」要先理解自然语言 —— 那属于 **P9 的 LLM 阶段，这里刻意不做**：
   猜错会直接改变一条 MEDIUM 风险的判定，而抽不到只是少一个字段（规则侧会停在
   ``MISSING_INPUT``，不会被误判成"没超标"）。

已知缺口（v1 明确不做，记录在案）
------------------------------
* 只有**中文大写**没有阿拉伯数字的金额（``壹佰贰拾万元整``）不转换 —— 需要一套
  独立的大写金额解析，且黄金样例两种形式并存（有数值形式可用）。真实合同若只写大写，
  ``contract_amount`` 会缺失。
* 不带标签的信用代码（``某某公司（9131…）``）不抽取。
* ``payment_terms`` 只覆盖"带百分比"的付款计划；纯文字描述（"分三期支付"）不抽。
* ``prepay_ratio`` 只从**表格行**里读；正文句里的"预付款为合同总额的百分之三十"
  或"预付三成"**都不抽**（前者是没做，后者是刻意不做 —— 见保守选择 3）。
  真实合同若把比例写在正文或附件里，该字段会缺失 → 规则侧停在 ``MISSING_INPUT``。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.core.constants import ExtractMethod
from app.schemas.document import Paragraph, ParseResult
from app.schemas.understanding import MetadataItem

# --------------------------------------------------------------------------- #
# 字段目录：键 → (展示名, 值类型)
#
# **只在这一个地方定义** —— 展示名与类型不需要调用方再维护第二张表。
# 顺序即输出顺序（读起来与合同的行文顺序一致）。
# --------------------------------------------------------------------------- #
_FIELD_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("our_party_name", "我方主体名称", "TEXT"),
    ("counterparty_name", "相对方名称", "TEXT"),
    ("credit_code", "统一社会信用代码", "CODE"),
    ("contract_amount", "合同金额", "AMOUNT"),
    ("currency", "币种", "TEXT"),
    ("sign_date", "签订日期", "DATE"),
    ("effective_date", "生效日期", "DATE"),
    ("expire_date", "到期日期", "DATE"),
    ("payment_terms", "付款条件", "TEXT"),
    ("prepay_ratio", "预付款比例", "RATIO"),
)

#: 日期骨架：``2026 年 9 月 14 日`` / ``2026-09-14`` / ``2026/9/14``
_DATE = r"(?P<y>\d{4})\s*[年\-/]\s*(?P<m>\d{1,2})\s*[月\-/]\s*(?P<d>\d{1,2})\s*日?"

#: 「甲方（采购方）：启明数智科技（上海）有限公司」
#: ``[^：:]`` 限长 12 —— 挡住"甲方应当在收到货物后 30 日内：..." 这类正文句
_PARTY = re.compile(r"^(?P<role>甲方|乙方)[^：:]{0,12}[：:]\s*(?P<name>.+?)\s*$")

#: 信用代码**必须带标签**（见模块 docstring 的保守选择 2）
_CREDIT_CODE = re.compile(r"统一社会信用代码[：:]\s*(?P<code>[0-9A-HJ-NPQRTUWXY]{18})")

#: 币种词表 —— **外币排在人民币之前**：``美元`` 含 ``元``，顺序反了会全部判成 CNY
_CURRENCIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("USD", ("美元", "USD", "$")),
    ("EUR", ("欧元", "EUR", "€")),
    ("HKD", ("港元", "港币", "HKD")),
    ("CNY", ("人民币", "¥", "￥", "元")),
)

#: 金额的三种常见写法。只认人民币会让 ``currency`` 恒为 CNY，这个字段就没意义了。
_CURRENCY_SIGNS = r"[¥￥$€]"
#: 可以出现在数字**前面**的币种词。``元`` 不在其中 —— 「元 100」不是写法。
_CURRENCY_PREFIX = r"人民币|美元|欧元|港元|港币"
#: 可以出现在数字**后面**的币种词。``1200 元`` 是最常见的写法，必须收。
_CURRENCY_SUFFIX = rf"{_CURRENCY_PREFIX}|元"

_AMOUNT = re.compile(
    rf"{_CURRENCY_SIGNS}\s*(?P<sign>[\d,]+(?:\.\d+)?)"  # ¥1,200,000.00 / $100
    rf"|(?:{_CURRENCY_PREFIX})\s*(?P<prefix>[\d,]+(?:\.\d+)?)"  # 人民币 5000 / 美元 100
    rf"|(?P<suffix>[\d,]+(?:\.\d+)?)\s*(?:{_CURRENCY_SUFFIX})"  # 1200 元 / 100 美元
)

#: 日期字段：标签与日期之间**必须紧邻**（见模块 docstring 的保守选择 1）
_DATE_FIELDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sign_date", re.compile(r"(?:签订|签署|签约)日期[：:]?\s*" + _DATE)),
    ("effective_date", re.compile(r"(?:生效日期[：:]?\s*|自\s*)" + _DATE + r"\s*起生效")),
    ("expire_date", re.compile(r"(?:有效期至|到期日[：:]?|终止日期[：:]?)\s*" + _DATE)),
)

#: 付款条件的触发词
_PAYMENT_MARKER = re.compile(r"%|百分之")

#: 预付款行的标签词。只要求出现「预付」—— 表格里它就是标签单元格，
#: 不需要更严的模式（``1. 预付款`` / ``预付款`` / ``预付`` 都能认）。
_PREPAY_LABEL = re.compile(r"预付")

#: 百分比：``30%`` / ``30.5%``；全角 ``％`` 也认（合同里两种都常见）
_PERCENT = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*[%％]")


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def extract_metadata(parse_result: ParseResult) -> list[MetadataItem]:
    """从段落序列里抽取合同文档自身的元数据。

    **不抛异常**：抽不到就不产出该条，返回的列表可能为空。
    每条都带 ``paragraph_index`` 与 ``quote``，说明它从哪里读出来的。
    """
    paragraphs = parse_result.paragraphs
    if not paragraphs:
        return []

    found: dict[str, MetadataItem] = {}
    _collect_parties(paragraphs, found)
    _collect_credit_code(paragraphs, found)
    _collect_amount(paragraphs, found)
    _collect_dates(paragraphs, found)
    _collect_payment_terms(paragraphs, found)
    _collect_prepay_ratio(paragraphs, found)

    # 按字段目录的顺序输出，过滤掉没抽到的
    return [found[key] for key, _label, _value_type in _FIELD_CATALOG if key in found]


# --------------------------------------------------------------------------- #
# 各字段的抽取
# --------------------------------------------------------------------------- #
def _collect_parties(paragraphs: list[Paragraph], found: dict[str, MetadataItem]) -> None:
    """甲乙方名称。文档顺序第一个命中的就是"主体声明"那一段，签署栏排在后面。"""
    wanted = {"甲方": "our_party_name", "乙方": "counterparty_name"}
    for paragraph in paragraphs:
        if paragraph.block_type != "PARAGRAPH":
            continue
        match = _PARTY.match(paragraph.text)
        if match is None:
            continue
        key = wanted[match.group("role")]
        if key in found:
            continue
        name = match.group("name").strip()
        if not name:
            continue
        found[key] = _item(key, name, paragraph, quote=paragraph.text)


def _collect_credit_code(paragraphs: list[Paragraph], found: dict[str, MetadataItem]) -> None:
    """统一社会信用代码。多个候选时取**文档里第一个**（不做主体归属判断）。"""
    for paragraph in paragraphs:
        match = _CREDIT_CODE.search(paragraph.text)
        if match is None:
            continue
        found["credit_code"] = _item("credit_code", match.group("code"), paragraph, quote=match.group(0))
        return


def _collect_amount(paragraphs: list[Paragraph], found: dict[str, MetadataItem]) -> None:
    """合同金额及其币种。

    币种**只在金额所在段落里找** —— 全文档搜索会把别的币种（比如报价附件里的美元）
    错配到本合同金额上。
    """
    for paragraph in paragraphs:
        match = _AMOUNT.search(paragraph.text)
        if match is None:
            continue
        raw = next(g for g in match.groups() if g)
        amount = _normalize_amount(raw)
        if amount is None:
            continue
        found["contract_amount"] = _item("contract_amount", amount, paragraph, quote=match.group(0))

        currency = _detect_currency(paragraph.text)
        if currency is not None:
            found["currency"] = _item("currency", currency, paragraph, quote=paragraph.text)
        return


def _collect_dates(paragraphs: list[Paragraph], found: dict[str, MetadataItem]) -> None:
    """三个日期字段。每个字段独立扫描，日期必须紧邻各自的标签。"""
    for key, pattern in _DATE_FIELDS:
        for paragraph in paragraphs:
            match = pattern.search(paragraph.text)
            if match is None:
                continue
            found[key] = _item(key, _format_date(match), paragraph, quote=match.group(0))
            break


def _collect_payment_terms(paragraphs: list[Paragraph], found: dict[str, MetadataItem]) -> None:
    """付款条件：第一处含百分比的**内容块**。

    表格行按"连续块"整块取（付款计划表就是连续几行）；普通段落取它自己。
    """
    for i, paragraph in enumerate(paragraphs):
        if _PAYMENT_MARKER.search(paragraph.text) is None:
            continue

        if paragraph.block_type == "TABLE_ROW":
            start = i
            while start > 0 and paragraphs[start - 1].block_type == "TABLE_ROW":
                start -= 1
            end = i
            while end + 1 < len(paragraphs) and paragraphs[end + 1].block_type == "TABLE_ROW":
                end += 1
            block = paragraphs[start : end + 1]
            found["payment_terms"] = _item(
                "payment_terms",
                "\n".join(p.text for p in block),
                paragraphs[start],
                quote="\n".join(p.text for p in block),
            )
        else:
            found["payment_terms"] = _item("payment_terms", paragraph.text, paragraph, quote=paragraph.text)
        return


def _collect_prepay_ratio(paragraphs: list[Paragraph], found: dict[str, MetadataItem]) -> None:
    """预付款比例：**表格行**里"标签含预付款 + 同行有百分比"的第一处。

    为什么只认表格行：付款计划表里「预付款 | 30%」是**结构化事实** ——
    标签与比例在同一行的固定位置，读出来不需要理解语义（见模块 docstring 的保守选择 3）。
    正文句里的写法一律不认，那属于 P9 的 LLM 阶段。

    与 ``payment_terms`` 的关系：那条抽的是**整块原文**（供人工核对），
    这条抽的是其中的**一个数值**（供规则比较）。两者各取各的第一处，互不影响 ——
    也正因为如此，本函数**不读** ``found["payment_terms"]``。
    """
    for paragraph in paragraphs:
        if paragraph.block_type != "TABLE_ROW":
            continue
        if _PREPAY_LABEL.search(paragraph.text) is None:
            continue

        match = _PERCENT.search(paragraph.text)
        if match is None:
            # 标签行里没有合法百分比 —— 这一行不产出，继续往后找（"第一处**确定**匹配"）
            continue

        found["prepay_ratio"] = _item(
            "prepay_ratio",
            _percent_to_ratio(match.group("num")),
            paragraph,
            quote=paragraph.text,
        )
        return


def _percent_to_ratio(percent: str) -> str:
    """``"30"`` → ``"0.3"``；``"30.5"`` → ``"0.305"``。

    规范化成**纯数字串**（与 ``contract_amount`` 同一风格），下游 ``Decimal(value)``
    直接可比。``format(..., "f")`` 是为了绝不退化成科学计数法（``Decimal.normalize()``
    在大数上会产出 ``1E+2`` 这种形式）。
    """
    return format((Decimal(percent) / 100).normalize(), "f")


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _item(field_key: str, value: str, paragraph: Paragraph, *, quote: str) -> MetadataItem:
    label, value_type = next((lb, vt) for key, lb, vt in _FIELD_CATALOG if key == field_key)
    return MetadataItem(
        field_key=field_key,
        field_label=label,
        field_value=value,
        value_type=value_type,
        paragraph_index=paragraph.index,
        quote=quote,
        extract_method=ExtractMethod.REGEX.value,
    )


def _normalize_amount(raw: str) -> str | None:
    """把 ``1,200,000.00`` 规范成 ``1200000.00``（两位小数的纯数字串）。

    与项目「金额一律 DECIMAL(18,2)」的约定对齐 —— 下游拿到就能直接转 Decimal，
    不必各自去逗号和货币符号。
    """
    try:
        return f"{Decimal(raw.replace(',', '')).quantize(Decimal('0.01'))}"
    except (InvalidOperation, ValueError):
        return None


def _format_date(match: re.Match[str]) -> str:
    """规范成 ISO ``YYYY-MM-DD``。"""
    return f"{int(match.group('y')):04d}-{int(match.group('m')):02d}-{int(match.group('d')):02d}"


def _detect_currency(text: str) -> str | None:
    for code, tokens in _CURRENCIES:
        if any(token in text for token in tokens):
            return code
    return None


__all__ = ["extract_metadata"]
