"""规则审查能力域（P8）。

把「规则目录里的规则」变成「风险结果」：

* ``schemas``   —— Agent 侧规则模型与风险结果契约；Backend → Agent 的映射
  （``rule_from_backend``）
* ``evaluator`` —— **纯函数**求值器：``evaluate_rule(rule, clauses)``

**规则数据归 Backend、求值引擎归 Agent**（架构文档 §17 的 P8 行）。
这一层不认识 State、不认识 Graph、不访问 Backend —— 因此能脱离整张图单独测试。

⚠️ P8-1 只到求值器为止：``rule_review`` 节点、从 Backend 拉规则、
把结果写进 State 都属于 **P8-2**。
"""

from app.rules.evaluator import evaluate_rule
from app.rules.schemas import (
    AgentRule,
    EvaluationFailureReason,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleRisk,
    rule_from_backend,
)

__all__ = [
    "AgentRule",
    "EvaluationFailureReason",
    "RuleEvaluationResult",
    "RuleEvaluationStatus",
    "RuleRisk",
    "evaluate_rule",
    "rule_from_backend",
]
