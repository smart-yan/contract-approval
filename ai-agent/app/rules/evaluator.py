"""规则求值（P8-1）。

职责边界
-------
::

    AgentRule + list[Clause]  →  RuleEvaluationResult

**纯函数**：不发 HTTP、不碰 SQLAlchemy / FastAPI / LangGraph / LLM / Prompt、
不做文件 IO、没有可变全局状态。给它一条规则与**已识别出的合同结构**，
它回答一件事：

    这条规则**命中**、**未命中**，还是**无法求值**？

规则数据从哪来（Backend）与怎么用（Agent）在这一层彻底分开 ——
本模块**不认识 Backend**，只认识 :class:`~app.rules.schemas.AgentRule`。
这也是它能被纯单元测试完整覆盖的原因。

⚠️ 与 P7-3 ``state["keywords"]`` 无关
------------------------------------
``understanding/keywords.py`` 回答的是"**这份文档里出现了哪些主题词**"，
词表写在 Agent 的代码常量里；P8 的 KEYWORD 规则回答的是"**某条规则是否因关键词触发**"，
关键词在 ``review_rule.expression`` 里、由业务配置。
两者词表不同、作用域不同、输出不同（详见 ``keywords.py`` 的对照表）。
**本模块不读 ``state["keywords"]``** —— 混用就是两套会各自漂移的判定。

当前支持情况
-----------
========================  ==========================================================
``KEYWORD``               已实现：``{"keywords": [...], "logic": "ANY"}``
``REGEX``                 已实现（最小）：``{"pattern": "<正则>"}``
``EXISTS`` / ``MISSING``  **未实现**：架构文档 §7.2 未给出 ``expression`` 契约 →
                          ``EVALUATION_FAILED`` / ``UNSUPPORTED_RULE_TYPE``
``THRESHOLD``             表达式的**形状**已识别，但当前输入没有数值来源 →
                          ``EVALUATION_FAILED`` / ``MISSING_INPUT``
不认识的 ``rule_type``      ``EVALUATION_FAILED`` / ``UNSUPPORTED_RULE_TYPE``
========================  ==========================================================

**为什么 EXISTS/MISSING/THRESHOLD 是"失败"而不是"未命中"**：它们不是"没算出结果"，
而是"我们还没有求值它们的手段"。把缺手段表达成"未命中"，等于宣称一份必备条款缺失
的合同没问题 —— 那是**静默漏报**。宁可让结果显式地不可用，也不假装判断过了。

**为什么不自行发明表达式**：§11.1 列了 10 条 MISSING 规则，但 ``expression`` 长什么样
文档里一个字都没有。在此刻发明一套 DSL，等于把"猜"固化成契约 —— 而规则数据是要落库、
要被人在管理界面配置的，猜错以后要迁移的是数据。所以留成显式的 failure，
等它自己的契约确定（P9）再实现。

**``target_clause_types`` 里的未知类型同样是"无法求值"**：它的作用范围解释不了，
却不会匹配上任何 Clause —— 若不检查，整条规则会静默变成 ``NOT_MATCHED``
（= "合同确定没有风险"）。见 :func:`_validate_target_clause_types`。

位置契约（沿用 P6-2 / P7-1，不重新设计）
--------------------------------------
``paragraph_index`` + ``quote`` 必须能追溯到真实文档内容：

* :class:`~app.schemas.understanding.Clause` 只给了 ``[start_paragraph_index,
  end_paragraph_index]`` 与 ``text``，而 ``text`` 由 P7-1 保证是
  ``"\\n".join(p.text for p in paragraphs[start:end+1])``，且段落文本内不含 ``\\n``
  —— 因此 ``text.split("\\n")`` 的第 i 行**就是**段落 ``start_paragraph_index + i``。
  见 :func:`_iter_lines`。本轮**不引入 char offsets**。
* ``original_text`` 直接取该段落原文，``quote`` 取其中的逐字片段 ——
  两者都是"抄"来的，不是"写"出来的。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from app.core.constants import ClauseType, RuleType
from app.rules.schemas import (
    AgentRule,
    EvaluationFailureReason,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleRisk,
)
from app.schemas.understanding import Clause

#: KEYWORD 目前唯一实现的组合逻辑。``logic`` 出现其它取值时**求值失败**而不是退化成
#: ANY —— 把 ``ALL`` 当成 ``ANY`` 会凭空多报风险，这是最不该有的错误。
_SUPPORTED_KEYWORD_LOGIC = "ANY"

#: Agent 认识的条款类型取值。取自 :class:`~app.core.constants.ClauseType` 本身，
#: 而不是再抄一份字符串 —— 那份枚举已被 ``test_understanding_contract.py`` 钉死为 11 个值，
#: 因此"合法类型的集合"只有一个来源，不会与它漂移。
_KNOWN_CLAUSE_TYPES = frozenset(member.value for member in ClauseType)


class _InvalidExpression(Exception):
    """``expression`` 无法被本求值器解释。

    这是**内部**控制流，只在 :mod:`app.rules.evaluator` 内部抛出与捕获，
    永远不会逃出 :func:`evaluate_rule` —— 调用方看到的是
    ``EVALUATION_FAILED`` + 一条人话 message。
    """


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def evaluate_rule(rule: AgentRule, clauses: Sequence[Clause]) -> RuleEvaluationResult:
    """求值一条规则。**不抛异常**（见下方"异常策略"）。

    :param rule: Agent 侧的规则模型（由 ``rule_from_backend`` 从 Backend 目录映射而来）
    :param clauses: **已识别出的**条款，顺序即文档顺序（``identify_clauses`` 的输出）。
        ``clauses`` 为空表示"文档里没有任何条款"——对 KEYWORD / REGEX 而言，
        这就是**未命中**（确实没找到），而不是无法求值
    :return: 三态结果；MATCHED 时 ``risks`` 按文档顺序排列

    异常策略
    -------
    **只吞自己认识的失败**：``expression`` 解释不了 → ``_InvalidExpression`` → 失败结果。
    其它异常（``AttributeError`` 之类）**不捕获** —— 那是求值器自己的 bug，
    伪装成"规则无法求值"只会让 bug 更难被发现。
    唯一的例外是正则编译失败，它是**输入**的问题（业务配置的正则写错了），
    因此被显式转成 ``_InvalidExpression``。

    校验顺序
    -------
    ``target_clause_types`` 的合法性**先于**任何求值被检查，因此它对所有 ``rule_type``
    一视同仁（包括 EXISTS / MISSING / 未知类型）。理由：这是**规则配置能不能被解释**的问题，
    优先于"这条规则有没有实现求值手段" —— 一份连作用范围都读不懂的规则，
    讨论它的求值手段没有意义。
    """
    invalid_scope = _validate_target_clause_types(rule)
    if invalid_scope is not None:
        return invalid_scope

    if rule.rule_type == RuleType.KEYWORD:
        return _evaluate_keyword(rule, clauses)
    if rule.rule_type == RuleType.REGEX:
        return _evaluate_regex(rule, clauses)
    if rule.rule_type == RuleType.THRESHOLD:
        return _evaluate_threshold(rule, clauses)
    if rule.rule_type in (RuleType.EXISTS, RuleType.MISSING):
        return _failed(
            rule,
            EvaluationFailureReason.UNSUPPORTED_RULE_TYPE,
            f"rule_type={rule.rule_type} 的 expression 契约尚未定义"
            "（架构文档 §7.2 只给了 KEYWORD 与 THRESHOLD 的示例），"
            "P8-1 不自行发明表达式语言，因此这条规则**无法求值** —— 注意这既不是命中，也不是未命中。",
        )
    return _failed(
        rule,
        EvaluationFailureReason.UNSUPPORTED_RULE_TYPE,
        f"Agent 求值器不认识 rule_type={rule.rule_type!r}"
        "（认识的取值见 core.constants.RuleType），该规则**无法求值**。",
    )


# --------------------------------------------------------------------------- #
# KEYWORD
# --------------------------------------------------------------------------- #
def _evaluate_keyword(rule: AgentRule, clauses: Sequence[Clause]) -> RuleEvaluationResult:
    """关键词命中：在目标条款的每一段里找 ``expression["keywords"]`` 里的词。"""
    try:
        terms = _parse_keyword_expression(rule.expression)
    except _InvalidExpression as exc:
        return _failed(rule, EvaluationFailureReason.INVALID_EXPRESSION, str(exc))

    risks: list[RuleRisk] = []
    for clause in _target_clauses(rule, clauses):
        for paragraph_index, text in _iter_lines(clause):
            hits = _keywords_in(text, terms)
            if not hits:
                continue
            # 同一段落里命中多个关键词时**只产出一条风险**：风险的含义是
            # "这条规则在这个位置被触发了"，同一规则同一段落报多条只是重复计数。
            # quote 取**最靠左**的那个（按在原文中的位置），便于前端高亮。
            risks.append(
                _build_risk(
                    rule,
                    paragraph_index=paragraph_index,
                    original_text=text,
                    quote=hits[0],
                    reason="命中规则关键词：" + "、".join(f"「{term}」" for term in hits),
                )
            )

    return _matched(rule, risks) if risks else _not_matched(rule)


def _parse_keyword_expression(expression: Mapping[str, Any]) -> list[str]:
    """解析 ``{"keywords": [...], "logic": "ANY"}``，非法即抛 :class:`_InvalidExpression`。"""
    logic = expression.get("logic", _SUPPORTED_KEYWORD_LOGIC)
    if logic != _SUPPORTED_KEYWORD_LOGIC:
        raise _InvalidExpression(
            f"expression.logic={logic!r} 未实现（P8-1 只实现 {_SUPPORTED_KEYWORD_LOGIC}）。"
            "**不能退化成 ANY** —— 多关键词取并集会凭空多报风险。"
        )

    raw_keywords = expression.get("keywords")
    if not isinstance(raw_keywords, list):
        raise _InvalidExpression('expression 缺少 keywords 数组（契约：{"keywords": [...], "logic": "ANY"}）')

    terms: list[str] = []
    for item in raw_keywords:
        if not isinstance(item, str) or not item:
            raise _InvalidExpression(f"keywords 里的元素必须是非空字符串，实际是 {item!r}")
        if item not in terms:  # 去重但**保持 expression 里的顺序**，保证结果可复现
            terms.append(item)

    if not terms:
        raise _InvalidExpression("keywords 为空数组 —— 这条规则没有任何可匹配的词，属于配置错误")
    return terms


def _keywords_in(text: str, terms: Sequence[str]) -> list[str]:
    """``text`` 中命中的关键词，**按出现位置从左到右**排序（同位置按词表顺序）。"""
    return sorted((term for term in terms if term in text), key=text.index)


# --------------------------------------------------------------------------- #
# REGEX
# --------------------------------------------------------------------------- #
def _evaluate_regex(rule: AgentRule, clauses: Sequence[Clause]) -> RuleEvaluationResult:
    """正则命中：在目标条款的每一段里 ``search`` ``expression["pattern"]``。"""
    try:
        pattern = _compile_pattern(rule.expression)
    except _InvalidExpression as exc:
        return _failed(rule, EvaluationFailureReason.INVALID_EXPRESSION, str(exc))

    risks: list[RuleRisk] = []
    for clause in _target_clauses(rule, clauses):
        for paragraph_index, text in _iter_lines(clause):
            match = pattern.search(text)
            if match is None:
                continue

            quote = match.group(0)
            # 零宽匹配（如 ``\d*``）命中的是空串：没有可引用的原文，
            # ``quote`` 会是 ""，等于造一条无法人工核对的证据。跳过它。
            if not quote:
                continue

            # 同段落只产出一条（与 KEYWORD 同口径），取第一次匹配。
            risks.append(
                _build_risk(
                    rule,
                    paragraph_index=paragraph_index,
                    original_text=text,
                    quote=quote,
                    reason=f"命中规则正则（{pattern.pattern}）：「{quote}」",
                )
            )

    return _matched(rule, risks) if risks else _not_matched(rule)


def _compile_pattern(expression: Mapping[str, Any]) -> re.Pattern[str]:
    """编译 ``{"pattern": "<正则>"}``。

    ⚠️ REGEX 的 ``expression`` 形状是 **P8-1 定的**：架构文档 §7.2 只给了 KEYWORD
    与 THRESHOLD 的示例，§11.1 虽列了几条 REGEX 规则，但没写表达式长什么样。
    这里取最小的 ``pattern`` 键 —— 将来规则管理的契约若定成别的键，
    **只需要改这一个函数**。
    """
    raw_pattern = expression.get("pattern")
    if not isinstance(raw_pattern, str) or not raw_pattern:
        raise _InvalidExpression(
            'expression 缺少非空字符串 pattern（P8-1 的 REGEX 契约：{"pattern": "..."}）'
        )
    try:
        return re.compile(raw_pattern)
    except re.error as exc:
        # 非法正则**不是**"未命中"：调用方要能区分"这条规则写错了"与"没匹配上"。
        raise _InvalidExpression(f"expression.pattern 不是合法正则：{exc}") from exc


# --------------------------------------------------------------------------- #
# THRESHOLD
# --------------------------------------------------------------------------- #
def _evaluate_threshold(rule: AgentRule, clauses: Sequence[Clause]) -> RuleEvaluationResult:
    """数值比较：**当前无值可算，恒为 MISSING_INPUT**。

    这不是没写完，而是输入里确实没有数值来源：本函数的入参只有规则与条款，
    而 :func:`evaluate_rule` 的契约（P8-1）**不引入任何 metadata 推导**
    ``expression["field"]`` 指向的字段（如 ``prepay_ratio``）恰恰是 P7-2
    **刻意没有抽取**的（GAP-C：它要从"预付 30%"这类描述里算出比例，
    属于派生指标，不是文档里写着的事实）。

    所以这里做两件事：
    1. 校验 ``expression`` 的**形状**（缺字段 / 类型不对 → INVALID_EXPRESSION）
    2. 形状合法但无值可算 → MISSING_INPUT，并把缺的那个字段名说清楚

    **绝不能返回 NOT_MATCHED** —— 那等于宣称"预付款比例没超标"，
    而我们根本没有算过它。
    """
    try:
        field = _parse_threshold_expression(rule.expression)
    except _InvalidExpression as exc:
        return _failed(rule, EvaluationFailureReason.INVALID_EXPRESSION, str(exc))

    # ``clauses`` 在这个分支用不上，但签名与其它求值器保持一致 ——
    # "有没有可用的值"的答案只应该在求值器内部给出，而不是让调用方先判断一次。
    return _failed(
        rule,
        EvaluationFailureReason.MISSING_INPUT,
        f"expression 要求比较字段 {field!r} 的数值，但 P8-1 的求值输入只有「规则 + 已识别条款」，"
        f"没有任何可计算出 {field!r} 的数值来源（GAP-C：该字段未被 P7-2 抽取，"
        "派生指标需要单独设计）。**这是无法求值，不是未命中**。",
    )


def _parse_threshold_expression(expression: Mapping[str, Any]) -> str:
    """校验 ``{"field": ..., "op": ..., "value": ...}`` 的形状，返回 ``field``。

    只校验**形状**，不校验 ``op`` 的取值集合 —— 取值集合属于将来那个"提供数值"的
    那一层（它才知道支持哪些比较），在这里钉死一份只会多一处会漂移的声明。
    """
    field = expression.get("field")
    if not isinstance(field, str) or not field:
        raise _InvalidExpression(
            'expression 缺少非空字符串 field（契约：{"field": ..., "op": ..., "value": ...}）'
        )

    op = expression.get("op")
    if not isinstance(op, str) or not op:
        raise _InvalidExpression("expression 缺少非空字符串 op（如 gt / gte / lt）")

    value = expression.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # bool 是 int 的子类，必须显式挡掉 —— 否则 True 会被当成 1 通过
        raise _InvalidExpression(f"expression.value 必须是数字，实际是 {value!r}")

    return field


# --------------------------------------------------------------------------- #
# 内部：条款筛选、位置、结果构造
# --------------------------------------------------------------------------- #
def _validate_target_clause_types(rule: AgentRule) -> RuleEvaluationResult | None:
    """``target_clause_types`` 里出现 Agent 不认识的条款类型 → **无法求值**。

    为什么不能放过它
    --------------
    未知类型不会匹配上任何 ``Clause``，于是整条规则静默变成 ``NOT_MATCHED`` ——
    而 ``NOT_MATCHED`` 的含义是"**合同确定没有这个风险**"。事实却是
    "这条规则的作用范围没人能解释"。这是最典型的静默漏报：
    配置写错了，报告上却显示一切正常。

    **不归一化、不映射别名、不猜测、不自动修正**：``"liability"`` 不会被转成
    ``"LIABILITY"`` 再匹配。归一化会把一个配错的值悄悄变成一个能用的值 ——
    配错从此再也不会被发现，而它该被修的是 Backend 里的规则配置。
    因此这里只报告，不修复。

    取值范围来自 :data:`_KNOWN_CLAUSE_TYPES`（= ``core.constants.ClauseType`` 的 11 个值）。

    ``None`` / ``[]`` 都**不是**错误：它们表示"不限"，与 :func:`_target_clauses` 同口径。
    """
    targets = rule.target_clause_types
    if not targets:
        return None

    unknown = [target for target in targets if target not in _KNOWN_CLAUSE_TYPES]
    if not unknown:
        return None

    return _failed(
        rule,
        EvaluationFailureReason.INVALID_TARGET_CLAUSE_TYPE,
        f"target_clause_types 含 Agent 不认识的条款类型 {unknown}"
        "（合法取值见 core.constants.ClauseType，注意大小写敏感）。"
        "这条规则的**作用范围无法解释**，因此它既不是命中，也不是未命中 —— "
        "请修正 Backend 的规则配置，不要指望它自己会生效。",
    )


def _target_clauses(rule: AgentRule, clauses: Sequence[Clause]) -> Iterable[Clause]:
    """规则作用范围内的条款。

    ``target_clause_types`` 为 ``None`` **或空列表**都表示不限 —— 后者是刻意的：
    空列表如果被当成"作用范围是空的"，一条配错的规则会静默地永不触发，
    而配置者的本意显然是"不限制"。判定用**精确字符串比较**（取值同
    ``core.constants.ClauseType``），不做大小写或别名归一化 —— 归一化会掩盖
    规则配错，而配错应当被发现。

    ⚠️ 前置条件：``target_clause_types`` 必须已经被 :func:`_validate_target_clause_types`
    判定为合法（``evaluate_rule`` 在分派前统一做掉）。本函数**只负责筛选**，
    不重复校验 —— 一个判断只在一个地方做。
    """
    targets = rule.target_clause_types
    if not targets:
        return clauses
    return [clause for clause in clauses if clause.clause_type in targets]


def _iter_lines(clause: Clause) -> Iterator[tuple[int, str]]:
    """枚举条款覆盖的 ``(段落序号, 段落文本)``。

    这是把 P7 的契约**用出来**而不是重新设计：``Clause.text`` 是连续段落文本以
    ``\\n`` 拼接（P7-1），且段落文本内不含 ``\\n``（P6-2），
    因此第 i 行**就是**段落 ``start_paragraph_index + i``。
    与 ``MetadataItem.quote`` 对块级字段的约定（行 i ↔ 段落 ``paragraph_index + i``）同源。
    """
    for offset, text in enumerate(clause.text.split("\n")):
        yield clause.start_paragraph_index + offset, text


def _build_risk(
    rule: AgentRule,
    *,
    paragraph_index: int,
    original_text: str,
    quote: str,
    reason: str,
) -> RuleRisk:
    """组装一条风险结果。``original_text`` / ``quote`` 必须是**文档里已有的文本**。

    ``source`` 不在这里赋值：它在 :class:`RuleRisk` 上被声明为 ``Literal["RULE"]``，
    由模型自己保证 —— 求值器想写出别的来源都写不出来。
    """
    return RuleRisk(
        risk_code=rule.rule_code,
        risk_title=rule.rule_name,
        dimension=rule.dimension,
        risk_level=rule.severity,
        reason=reason,
        legal_basis=rule.legal_basis,
        original_text=original_text,
        paragraph_index=paragraph_index,
        quote=quote,
    )


def _matched(rule: AgentRule, risks: list[RuleRisk]) -> RuleEvaluationResult:
    return RuleEvaluationResult(
        rule_code=rule.rule_code,
        rule_name=rule.rule_name,
        status=RuleEvaluationStatus.MATCHED,
        risks=risks,
    )


def _not_matched(rule: AgentRule) -> RuleEvaluationResult:
    """**求值成功且没有命中** —— 这是一个确定的判断，不带任何 failure 字段。"""
    return RuleEvaluationResult(
        rule_code=rule.rule_code,
        rule_name=rule.rule_name,
        status=RuleEvaluationStatus.NOT_MATCHED,
    )


def _failed(
    rule: AgentRule,
    reason: EvaluationFailureReason,
    message: str,
) -> RuleEvaluationResult:
    """**没能求值** —— 既不是命中，也不是未命中。"""
    return RuleEvaluationResult(
        rule_code=rule.rule_code,
        rule_name=rule.rule_name,
        status=RuleEvaluationStatus.EVALUATION_FAILED,
        failure_reason=reason,
        failure_message=message,
    )


__all__ = ["evaluate_rule"]
