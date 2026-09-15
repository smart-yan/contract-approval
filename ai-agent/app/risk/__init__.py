"""风险统一能力域（P9-8 建模 / P9-9 合并）。

把**两个互不相识的来源**收敛成同一种形状，再把**确实同一条**的收敛成一条：

::

    RuleRisk（P8-1 规则求值）        ResolvedFinding（P9-4 模型发现 + 定位）
                  │                                │
                  └──────── unify.py ──────────────┘
                                ▼
                        AgentRiskItem（schemas.py）
                                │  merge.py
                                ▼
                     去重后的风险清单（可能出现 RULE+LLM）

* ``schemas`` —— 统一风险项的**契约**（字段口径、与 Backend 的对齐与差异）
* ``unify``   —— 两个**纯映射函数**（不做合并、不去重、不打分）
* ``merge``   —— **保守的**合并（只认确定性身份，见该模块头部）

⚠️ 这一层仍然是**纯领域逻辑**：不接 Graph、不碰 State、不落库、不发 HTTP。
合并结果目前**没有消费者** —— 接入是后续步骤。
"""

from app.risk.merge import merge_risk_items, normalize_risk_title
from app.risk.schemas import AgentRiskItem
from app.risk.unify import unify_llm_finding, unify_rule_risk

__all__ = [
    "AgentRiskItem",
    "merge_risk_items",
    "normalize_risk_title",
    "unify_llm_finding",
    "unify_rule_risk",
]
