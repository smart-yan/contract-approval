"""``merge_risks`` 节点 —— 把两条来源收敛成**一份**统一风险列表（P9-9 接入）。

调用链
-----
::

    merge_risks Node                  →  risk.unify.unify_rule_risk()
    （State 编解码 + 拼接 + 调用）        risk.unify.unify_llm_finding()
                                      →  risk.merge.merge_risk_items()

节点**只做 State 编解码**，与 ``rule_review`` / ``llm_review`` 同构：

* 从 State 取出两条来源（``rule_risks`` / ``llm_findings``）
* 分别过**已有的**映射函数，转成统一的 ``AgentRiskItem``
* 拼接后交给**已有的**合并函数
* 把结果写进 ``risks``

字段映射一条都不在这里（P9-8 的 ``unify``），合并判断一条都不在这里
（P9-9 的 ``merge``）—— 那些已经独立通过 Review。节点里重写一遍，
就等于把第二套会漂移的映射与判定放进系统，而且**跑题时不会有任何测试报错**
（两套实现各自自洽）。

为什么不把 unify 与 merge 合成一个函数
------------------------------------
「模型发现 → 风险项」与「多条风险 → 去重后的风险」是两件事，失败与调试的粒度也不同：
合并测出问题时，要能一眼看出是"映射搬错了字段"还是"合并判错了身份"。
节点这一层薄到只剩编解码，正是为了这个。

这一层的失败语义：**没有失败**
--------------------------
合并永远不会让整次审查失败，因此这个节点**不写 ``error_code``**，也不抛异常：

* LLM 降级（``llm_findings`` 缺失）→ 只剩规则风险，照常合并
* 两侧都没东西（规则没命中 + 模型没报）→ ``risks=[]``，这是**正常结论**
  （"合并跑过了，没有风险"），不是"我们没跑"
* 规则审查失败（``error_code`` 已由 ``rule_review`` 写下）→ 这里照常执行，
  只是规则侧没有输入；**降级与失败分属两条通道**这件事由此保持不变：
  ``llm_error_code`` / ``error_code`` 各由产出它们的节点负责，本节点只读不写

⚠️ 顺带说明 ``risks`` 的可信度：它的**存在**表示合并跑过，但**不表示这次审查成功** ——
调用方仍须看 ``error_code``（规则审查失败时，图并没有停，合并照常执行，
只是规则侧为空）。这与 ``rule_risks`` 的口径一致：字段有值 ≠ 整次审查可用。

日志记在这里，不记在领域层
------------------------
"这次合并并掉了多少条、两条来源各有多少"是**可观测性**，属于边界（节点）的职责。
领域层 ``app/risk/merge.py`` 是纯函数，**刻意不记日志** —— 那里的"放弃关联"等
分支是静默的，观测点统一放在这里。
"""

from __future__ import annotations

import logging

from app.core.constants import RiskSource
from app.graph.state import ContractReviewState
from app.risk.merge import merge_risk_items
from app.risk.schemas import AgentRiskItem
from app.risk.unify import unify_llm_finding, unify_rule_risk

logger = logging.getLogger(__name__)


def merge_risks(state: ContractReviewState) -> dict[str, object]:
    """两条来源 → 统一风险列表，写回 ``risks``。

    * 读 State：``rule_risks``、``llm_findings``
    * 写 State：``risks``（``list[AgentRiskItem]`）

    ⚠️ 位置信息的来源不能搞错：模型风险的 ``paragraph_index`` 一律取
    ``ResolvedFinding`` 上**已经核过**的那一份（P9-7 的定位结果），
    不从 ``ResolvedFinding.finding`` 反推 —— 模型手里根本没有坐标系。
    这件事由 ``unify_llm_finding`` 保证，节点只是调用它，**不自己取字段**。

    ⚠️ 输入**只读**：``rule_risks`` / ``llm_findings`` 原样留在 State 里，
    它们既是下游回溯的依据，也是"这条风险为什么成立"的证据链。
    """
    rule_risks = state.get("rule_risks") or []
    findings = state.get("llm_findings") or []

    # 先规则、后模型：规则是**确定性**的那一侧，合并在两侧都命中时以它为锚点，
    # 因此它的顺序天然该排在前面（`merge_risk_items` 的锚点选择也依赖输入顺序）。
    unified: list[AgentRiskItem] = [
        *(unify_rule_risk(risk) for risk in rule_risks),
        *(unify_llm_finding(resolved) for resolved in findings),
    ]

    risks = merge_risk_items(unified)

    logger.info(
        "merge_risks 完成 | file_id=%s rule_risks=%d llm_findings=%d unified=%d merged=%d "
        "rule_and_llm=%d llm_only=%d",
        state.get("file_id"),
        len(rule_risks),
        len(findings),
        len(unified),
        len(risks),
        sum(1 for risk in risks if risk.source is RiskSource.RULE_AND_LLM),
        sum(1 for risk in risks if risk.source is RiskSource.LLM),
    )
    return {"risks": risks}


__all__ = ["merge_risks"]
