"""风险统一能力域（P9-8）。

把**两个互不相识的来源**收敛成同一种形状：

::

    RuleRisk（P8-1 规则求值）        ResolvedFinding（P9-4 模型发现 + 定位）
                  │                                │
                  └──────── unify.py ──────────────┘
                                ▼
                        AgentRiskItem（schemas.py）

* ``schemas`` —— 统一风险项的**契约**（字段口径、与 Backend 的对齐与差异）
* ``unify``   —— 两个**纯映射函数**（不做合并、不去重、不打分）

⚠️ 本轮**只到映射**。合并（``RULE`` + ``LLM`` → ``RULE+LLM``）、合并键、
严重度取高、评分都属于后续步骤 —— 本包目前没有任何函数产出 ``RULE+LLM``。
"""

from app.risk.schemas import AgentRiskItem
from app.risk.unify import unify_llm_finding, unify_rule_risk

__all__ = ["AgentRiskItem", "unify_llm_finding", "unify_rule_risk"]
