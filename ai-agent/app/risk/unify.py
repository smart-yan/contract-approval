"""两个来源 → 统一风险项（P9-8）。**纯映射，不做合并。**

::

    RuleRisk          ──unify_rule_risk()──▶      AgentRiskItem
    ResolvedFinding   ──unify_llm_finding()──▶     AgentRiskItem

**这里不含任何判断**：不去重、不比较、不合并、不提升等级、不打分 ——
它只回答"这个来源的这条风险，在统一形状里长什么样"。
两个函数都是**纯函数且不修改入参**（映射只读不写）。

为什么是两个函数而不是一个"万能转换"
----------------------------------
两个来源的**字段名与嵌套结构都不同**（``RuleRisk.risk_code`` vs
``ResolvedFinding.finding`` 里的一堆字段）。写成一个函数就得在里面按类型分支，
那是把"两套来源的形状差异"藏进一个 if 里 —— 读代码的人再也看不出
"每个字段到底从哪来"。两个薄函数各自直白，加起来也不到一个屏幕。
"""

from __future__ import annotations

from app.core.constants import RiskSource
from app.llm.finding_resolution import ResolvedFinding
from app.risk.schemas import AgentRiskItem
from app.rules.schemas import RuleRisk


def unify_rule_risk(risk: RuleRisk) -> AgentRiskItem:
    """确定性规则风险 → 统一风险项。

    * ``source`` = :attr:`RiskSource.RULE` —— 由**这里**决定，不是从 ``RuleRisk.source``
      抄来的（那个字段是 P8-1 的类型级保证，这里的取值属于统一模型的口径）
    * ``risk_code`` ← ``rule_code``（规则来源**有**稳定编码）
    * ``dimension`` ← 规则目录里的维度，**原样搬运**（规则侧一直有值）
    * ``anchor_method`` / ``related_rule_code`` 均为 ``None``：
      规则风险有确定的段落号，不存在"反查方式"；它本身就是规则风险，
      不需要"关联到哪条规则"
    """
    return AgentRiskItem(
        source=RiskSource.RULE,
        risk_code=risk.risk_code,
        risk_title=risk.risk_title,
        dimension=risk.dimension,
        risk_level=risk.risk_level,
        reason=risk.reason,
        legal_basis=risk.legal_basis,
        original_text=risk.original_text,
        quote=risk.quote,
        paragraph_index=risk.paragraph_index,
        anchor_method=None,
        related_rule_code=None,
    )


def unify_llm_finding(resolved: ResolvedFinding) -> AgentRiskItem:
    """已定位的模型发现 → 统一风险项。

    * ``source`` = :attr:`RiskSource.LLM`（``RULE+LLM`` 是**合并**的产物，这里不产出）
    * ``risk_code`` = ``None`` —— 模型没有稳定编码，**也不许它编一个**
    * ``dimension`` ← ``.finding.dimension``（P9-8a 起是必填、受固定词表约束）：
      **原样搬运**。不许从 ``clause_type`` 反推（维度 ≠ 条款类型），
      更不许在这里硬编码一个默认值 —— 那会把"模型选错了维度"这种问题
      从数据里抹掉
    * ``original_text`` / ``quote`` / ``paragraph_index`` / ``anchor_method``
      一律取 :class:`~app.llm.finding_resolution.ResolvedFinding` 上**已经核过**的值：
      模型只给了 ``clause_index`` + ``quote``，位置是 Agent 定位出来的，
      因此不能从 ``.finding`` 里取（那里根本没有位置）
    * ``related_rule_code`` ← ``.finding.related_rule_code``：
      它**已经过 rule snapshot 核对**（核不上的在 P9-4 已被清空），这里可以直接搬运
    """
    return AgentRiskItem(
        source=RiskSource.LLM,
        risk_code=None,
        risk_title=resolved.finding.risk_title,
        dimension=resolved.finding.dimension,
        risk_level=resolved.finding.risk_level,
        reason=resolved.finding.reason,
        legal_basis=resolved.finding.legal_basis,
        original_text=resolved.original_text,
        quote=resolved.quote,
        paragraph_index=resolved.paragraph_index,
        anchor_method=resolved.anchor_method,
        related_rule_code=resolved.finding.related_rule_code,
    )


__all__ = ["unify_llm_finding", "unify_rule_risk"]
