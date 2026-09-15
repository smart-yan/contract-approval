"""风险合并：多条统一风险项 → 一份**不重复**的风险清单（P9-9）。

它在流水线上的位置
----------------
::

    RuleRisk ─┐
              ├─ unify（P9-8）─▶ AgentRiskItem[] ─▶ 本模块 ─▶ AgentRiskItem[]
    Finding ──┘                                            （可能出现 RULE+LLM）

合并**只做一件事**：把"确实同一条"的风险收敛成一条。它不评分、不分桶、
不排优先级、不生成建议、不落库 —— 那些是后续阶段。

为什么必须保守（本模块的最高原则）
--------------------------------
误合并**不可逆**：两条不同的风险并成一条后，被丢掉的那条的标题与理由
再也回不来，而人工审核看到的是一张"少了内容"的卡片 —— 而且没有任何信号
提示"这里本来有两条"。漏合并则相反：最坏情况是同一件事出现两次，
人工一眼就能看出重复。

所以本模块**只认确定性身份，不认相似度**：没有 embedding、没有 fuzzy、
没有相似度阈值、没有 LLM 二次判断、没有"看起来像同一个"的启发式。
**标题相似 / quote 相似 / dimension 相同 / 落在同一段** —— 单凭这些
一律不构成合并理由。

三条合并路径
-----------
====================  ==================================================
RULE ↔ RULE           ``risk_code`` 相同 **且** 落在同一段
RULE ↔ LLM            LLM 声称的 ``related_rule_code`` 指向该规则
                      **且** 落在同一段（见"关联必须是 1:1"）
LLM ↔ LLM             ``risk_title`` 规范化后相同 **且** 落在同一段
====================  ==================================================

三条路径**共同**的两个前提：``paragraph_index`` 相同、``dimension`` 相同。
前者是"同一处证据"，后者是"同一类关注点" —— 两者都对不上就不可能是同一条风险。
（维度对不上时**不合并**，而不是挑一个：挑一个就是猜。）

关联必须是 1:1
-------------
`RULE ↔ LLM` 走的是**模型的一句声称**（"我这条和规则 X 是同一件事"），
不是像 ``risk_code`` 那样的事实。因此当这句声称为**多义**时，合并会出错：

* 一条规则在同一段被**两个不同的**模型发现同时声称 → 二者矛盾，**不关联**
* 一个模型发现在同一段声称指向**两条不同的**规则 → 同样矛盾，**不关联**

矛盾时三方各自保留（宁可多一条重复，也不把两条不同的风险并成一条）。

锚点：合并后字段以谁为准
----------------------
合并出的条目要有一套**确定的**字段取值。本模块用"锚点"表达这件事：

* 合并单元里**有 RULE 风险** → 锚点是其中**第一条 RULE 风险**
  （规则是确定的、有稳定编码、可追溯到规则目录，字段口径以它为准）
* 否则 → 锚点是**第一条**风险（LLM ↔ LLM 时没有规则可以当锚点）

字段策略只有一条规则加两个例外：

* **锚点优先，空位补位**：锚点的字段一律保留；另一侧只在锚点该字段为
  ``None`` 时补进来。**绝不覆盖锚点的非空值**（不覆盖证据、不覆盖理由）。
* 例外一：``source`` —— 两侧都出现时取 :attr:`~app.core.constants.RiskSource.RULE_AND_LLM`
* 例外二：``risk_level`` —— 取两者**较高**等级（合并的意义之一就是别低估风险）

⚠️ ``risk_level`` 的"取高"只在两侧都是已知等级（HIGH / MEDIUM / LOW）时成立。
出现词表外的等级（规则目录配错才会有）时**不比较、不猜测**，保留锚点的值 ——
拿一个未知等级去和别人比高低，比出来的任何结果都是编的。

输入的契约：正常输入是 RULE / LLM，``RULE+LLM`` 只为幂等
------------------------------------------------------
``merge_risk_items()`` 的**正常输入是 RULE 与 LLM 两种原始风险**（``unify`` 的产物）。
``RULE+LLM`` **不是**这条流水线预期会喂进来的东西 —— 它是**本函数自己的输出**
（以及 :attr:`~app.core.constants.RiskSource` 里那个"合并之后才会出现"的取值）。

它被接受，**只为一件事：幂等**。来源已是 ``RULE+LLM`` 的条目原样透传，不再参与
任何匹配，于是 ``merge(merge(x)) == merge(x)`` —— 重复调用既不会把已经合并的结果
再并一次，也不会让它被第二次吸收进别的分组。这条性质是给"重跑 / 分片合并 /
上游重复投递"兜底的，**不是**在暗示调用方可以先自己合并一遍再送进来。

本模块**不记日志**（领域层保持纯粹）
----------------------------------
合并层是**纯领域逻辑**：无 IO、无 logging、无 HTTP、无数据库、
无全局可变状态、不修改入参、deterministic。日志是副作用，在这里记会让"纯函数"
这个性质名不副实，而"一次合并里放弃了哪些关联"属于**可观测性**，是调用方在边界上
（接 Graph 时）该做的事，不是领域层的职责。因此下面那些"放弃"分支全部是**静默**的
——它们的行为本身是确定的、可测的，只是不在这里留下日志。
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence

from app.core.constants import RiskSource
from app.risk.schemas import AgentRiskItem

#: 风险等级的高低序（用于合并时"取高"）。取值与 ``RiskLevelLiteral`` / Backend
#: ``RiskLevel`` 一致 —— 这里不新增词表，只是给已知的三个值排序。
_LEVEL_ORDER: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

#: 合并单元的键。形状随路径不同，只要能判等即可。
_UnitKey = tuple[Hashable, ...]


def normalize_risk_title(title: str) -> str:
    """``LLM ↔ LLM`` 身份用的规范化标题。

    **这是规范化，不是相似度**：只抹掉**不携带语义**的差异（空白与大小写），
    不做分词、不做词干、不做编辑距离。因此"知识产权归属供方"与
    "知识产权 归属供方" 视为同一条，而"知识产权归属供方"与
    "知识产权归属相对方"**永远**是两条 —— 少一个字也不行。

    它是本模块唯一的"身份判据"公开件，单独导出是为了让测试能钉住它。
    """
    return "".join(title.split()).casefold()


def merge_risk_items(items: Sequence[AgentRiskItem]) -> list[AgentRiskItem]:
    """把**确实同一条**的风险收敛成一条，返回新列表。

    :param items: 已经统一过的风险项（顺序有意义 —— 输出顺序 = 各合并单元
        **首次出现**的顺序，锚点也只与顺序有关）
    :return: 合并后的风险项；**单项单元原样返回**（不复制），
        多项单元是**新建的**对象

    纯函数：无 IO、无 logging、无 HTTP、无数据库、无全局可变状态、
    **不修改入参**、同样的输入永远得到同样的输出。

    不做的事：不评分、不分桶、不排序、不生成建议、不落库、不碰 Graph/State。
    """
    if not items:
        return []

    grouped = _group_by_unit(items)
    return [_merge_unit(items, indices) for indices in grouped.values()]


# --------------------------------------------------------------------------- #
# 分组：每个输入元素落在哪个合并单元里
# --------------------------------------------------------------------------- #
def _group_by_unit(items: Sequence[AgentRiskItem]) -> dict[_UnitKey, list[int]]:
    """输入下标 → 合并单元。``dict`` 的插入顺序即"首次出现顺序"。

    分两步是因为 ``RULE ↔ LLM`` 的关联依赖**分组结果**（要判断一条规则是否
    被唯一一个模型风险声称），不能在一趟里定型。
    """
    rule_groups, llm_groups, passthrough = _collect_groups(items)
    association = _associate(items, rule_groups, llm_groups)

    units: list[_UnitKey] = [("?",)] * len(items)

    for identity, indices in rule_groups.items():
        unit = _rule_unit(identity)
        for index in indices:
            units[index] = unit

    for identity, indices in llm_groups.items():
        # 关联上规则的模型风险**并入规则的单元**（同一个键），否则自己成组
        unit = _rule_unit(association[identity]) if identity in association else _llm_unit(identity)
        for index in indices:
            units[index] = unit

    for index in passthrough:
        # 已经合并过的条目：每一条自成一个单元，不再与任何东西匹配
        units[index] = ("merged", index)

    grouped: dict[_UnitKey, list[int]] = {}
    for index, unit in enumerate(units):
        grouped.setdefault(unit, []).append(index)
    return grouped


def _collect_groups(
    items: Sequence[AgentRiskItem],
) -> tuple[dict[tuple, list[int]], dict[tuple, list[int]], list[int]]:
    """按来源分桶：规则分组 / 模型分组 / 已合并（透传）。

    规则身份 = ``(risk_code, 段落, 维度)``；模型身份 = ``(规范化标题, 段落, 维度)``。
    段落与维度写进身份里，等于把"同一段 + 同一维度"这两个共同前提**结构性地**
    变成分组的必要条件 —— 对不上就进不了同一组，不需要事后再检查一遍。
    """
    rule_groups: dict[tuple, list[int]] = {}
    llm_groups: dict[tuple, list[int]] = {}
    passthrough: list[int] = []

    for index, item in enumerate(items):
        if item.source is RiskSource.RULE:
            rule_groups.setdefault(_rule_identity(item), []).append(index)
        elif item.source is RiskSource.LLM:
            llm_groups.setdefault(_llm_identity(item), []).append(index)
        else:
            passthrough.append(index)

    return rule_groups, llm_groups, passthrough


def _rule_identity(item: AgentRiskItem) -> tuple:
    return (item.risk_code, item.paragraph_index, item.dimension)


def _llm_identity(item: AgentRiskItem) -> tuple:
    return (normalize_risk_title(item.risk_title), item.paragraph_index, item.dimension)


def _rule_unit(identity: tuple) -> _UnitKey:
    return ("RULE", *identity)


def _llm_unit(identity: tuple) -> _UnitKey:
    return ("LLM", *identity)


# --------------------------------------------------------------------------- #
# RULE ↔ LLM 关联：1:1，多义即放弃
# --------------------------------------------------------------------------- #
def _associate(
    items: Sequence[AgentRiskItem],
    rule_groups: dict[tuple, list[int]],
    llm_groups: dict[tuple, list[int]],
) -> dict[tuple, tuple]:
    """模型分组 → 它**唯一**关联上的规则身份。

    只有当一个模型分组恰好指向一条规则、且那条规则恰好只被这一个模型分组
    指向时，才算关联成立。任何一侧出现多义都**放弃关联**（静默，见模块头部：
    本层不记日志）—— 挑一个会让"为什么这两条并了、那两条没并"变得无法解释。
    """
    claims_by_rule: dict[tuple, set[tuple]] = {}
    claims_by_llm: dict[tuple, set[tuple]] = {}

    for identity, indices in llm_groups.items():
        _, paragraph_index, dimension = identity
        for index in indices:
            code = items[index].related_rule_code
            if code is None:
                continue
            rule_identity = (code, paragraph_index, dimension)
            if rule_identity not in rule_groups:
                # 关联指向一条**本批次里不存在**的规则：不关联，也不臆造那条规则
                continue
            claims_by_rule.setdefault(rule_identity, set()).add(identity)
            claims_by_llm.setdefault(identity, set()).add(rule_identity)

    association: dict[tuple, tuple] = {}
    for rule_identity, llm_identities in claims_by_rule.items():
        if len(llm_identities) != 1:
            # 一条规则被多个**不同的**模型分组同时声称 → 矛盾 → 不关联
            continue
        llm_identity = next(iter(llm_identities))
        if len(claims_by_llm[llm_identity]) != 1:
            # 一个模型分组同时声称指向多条规则 → 同样矛盾 → 不关联
            continue
        association[llm_identity] = rule_identity

    return association


# --------------------------------------------------------------------------- #
# 合并一个单元
# --------------------------------------------------------------------------- #
def _merge_unit(items: Sequence[AgentRiskItem], indices: list[int]) -> AgentRiskItem:
    """把一个合并单元收敛成一条。

    锚点 = 单元里**第一条 RULE 风险**，没有 RULE 时 = 第一条风险。
    单项单元（含透传的 ``RULE+LLM``）**原样返回输入对象**，不复制。
    """
    anchor = next(
        (i for i in indices if items[i].source is RiskSource.RULE),
        indices[0],
    )
    merged = items[anchor]
    for index in indices:
        if index != anchor:
            merged = _merge_into(merged, items[index])
    return merged


def _merge_into(anchor: AgentRiskItem, other: AgentRiskItem) -> AgentRiskItem:
    """把 ``other`` 并进 ``anchor``（锚点），返回**新对象**。

    字段策略见模块头部："锚点优先，空位补位"，外加 ``source`` / ``risk_level``
    两个例外。证据字段（``original_text`` / ``quote`` / ``paragraph_index``）
    **一律取锚点的**：两者本来就是同一段落里的真实片段，无法客观判定谁"更优"，
    此时按裁决保守保留锚点值，不做挑选启发式。
    """
    return AgentRiskItem(
        source=_merged_source(anchor.source, other.source),
        risk_code=_prefer_anchor(anchor.risk_code, other.risk_code),
        risk_title=anchor.risk_title,
        dimension=anchor.dimension,
        risk_level=_higher_level(anchor.risk_level, other.risk_level),
        reason=anchor.reason,
        legal_basis=_prefer_anchor(anchor.legal_basis, other.legal_basis),
        original_text=anchor.original_text,
        quote=anchor.quote,
        paragraph_index=anchor.paragraph_index,
        anchor_method=_prefer_anchor(anchor.anchor_method, other.anchor_method),
        related_rule_code=_prefer_anchor(anchor.related_rule_code, other.related_rule_code),
    )


def _prefer_anchor(anchor_value, other_value):
    """锚点优先：锚点有值就用锚点的，**只有锚点为空时**才由另一侧补位。

    （这是"不覆盖"的实现 —— 反过来写就成了"另一侧优先覆盖"，
    证据与理由会在合并中悄悄被替换掉。）
    """
    return anchor_value if anchor_value is not None else other_value


def _merged_source(anchor_source: RiskSource, other_source: RiskSource) -> RiskSource:
    """两侧分别来自规则与模型时，合并结果的来源是 ``RULE+LLM``。

    只有真正发生了跨源合并且**两种来源都在**时才会出现它 —— 同源合并
    （RULE↔RULE / LLM↔LLM）保持原来的来源，不冒充跨源。
    """
    sources = {anchor_source, other_source}
    if RiskSource.RULE in sources and RiskSource.LLM in sources:
        return RiskSource.RULE_AND_LLM
    return anchor_source


def _higher_level(anchor_level: str, other_level: str) -> str:
    """取较高的风险等级；**只有真正合并时**才比较（单条风险不会被升级）。

    词表外的等级不参与比较：**保留锚点的值**。规则目录可以把 ``severity`` 配成
    任意字符串（数据库那一列是自由文本），拿它去和 HIGH/MEDIUM/LOW 比大小，
    比出来的任何结果都是编的 —— 不猜测，就保留锚点自己的声明。
    """
    anchor_rank = _LEVEL_ORDER.get(anchor_level)
    other_rank = _LEVEL_ORDER.get(other_level)

    if anchor_rank is None or other_rank is None:
        return anchor_level

    return anchor_level if anchor_rank >= other_rank else other_level


__all__ = ["merge_risk_items", "normalize_risk_title"]
