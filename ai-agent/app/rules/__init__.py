"""规则审查能力域（P8）。

把「规则目录里的规则」变成「风险结果」：

* ``schemas``   —— Agent 侧契约：规则模型、规则集快照、风险结果；规则映射
  （``rule_from_backend``）
* ``catalog``   —— Backend 规则目录响应 → ``RuleSetSnapshot``（纯映射，P8-2 第一小步）
* ``evaluator`` —— **纯函数**求值器：``evaluate_rule(rule, clauses)``

**规则数据归 Backend、求值引擎归 Agent**（架构文档 §17 的 P8 行）。
这一层不认识 State、不认识 Graph、不发 HTTP（HTTP 在 ``app/tools``）——
因此能脱离整张图单独测试。

⚠️ P8-2 的进度：规则集快照契约（本模块）与 ``graphs/nodes/rule_review.py`` 的
最小闭环已完成；**从 Backend 取规则**（谁把 ``rule_snapshot`` 放进 State）、
LLM 审查、风险合并仍未实现。
"""

from app.rules.catalog import RuleSnapshotError, snapshot_from_backend
from app.rules.evaluator import evaluate_rule
from app.rules.schemas import (
    AgentRule,
    EvaluationFailureReason,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleRisk,
    RuleSetSnapshot,
    rule_from_backend,
)

__all__ = [
    "AgentRule",
    "EvaluationFailureReason",
    "RuleEvaluationResult",
    "RuleEvaluationStatus",
    "RuleRisk",
    "RuleSetSnapshot",
    "RuleSnapshotError",
    "evaluate_rule",
    "rule_from_backend",
    "snapshot_from_backend",
]
