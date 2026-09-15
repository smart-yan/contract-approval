"""``llm_review`` 节点 —— 编排"让模型审一遍条款"这一步（P9-5 骨架）。

调用链
-----
::

    llm_review Node                →  llm.clause_review.review_clauses()
    （State 编解码 + 组装输入）        （业务层：提示 + Provider + 校验）

节点**只做 State 编解码**，与 ``identify_clauses`` / ``rule_review`` 同构：

* 从 State 里取出**已有的**条款与规则命中，组装成 :class:`ClauseReviewPromptInput`
* 调用业务层的 :func:`~app.llm.clause_review.review_clauses`
* 把拿回来的 findings 写进 ``llm_findings``

它**不**自己拼提示、不自己拼 JSON Schema、不碰 HTTP、不解析 JSON ——
那些分别是 ``prompts`` / ``json_guard`` / ``provider`` 的职责。
节点里若出现第二套提示或第二份 schema，两边就会各自漂移。

写进 State 的是**模型的原始发现**
-------------------------------
``llm_findings`` 里的元素是 :class:`~app.llm.findings.LLMFinding`：
只有"模型说了什么 + 它自己给的 ``clause_index`` / ``quote``"，
**没有**段落号、没有 ``source``、没有风险等级以外的加工。
定位（P9-3/P9-4）与合并（后续）是它们各自独立的一步 ——
本节点不提前替它们做决定。

⚠️ 本节点**尚未接入 Graph builder**（刻意的）
------------------------------------------
接上去以后，每一次真实审查都会跑到它，而端点的 ``ReviewContext`` 目前
**没有注入 LLM provider** —— 结果是所有真实请求都会被判成 ``LLM_UNAVAILABLE``
失败（``_to_response`` 会把 ``error_code`` 表达成 rejected）。
"什么时候开始要求 LLM、没配 provider 时整个流程该怎么办"是**接线那一步的裁决**，
不在本步（本步只建立节点的输入/输出契约）。

失败语义
-------
**不吞异常、也不让它冒泡炸掉整张图**：Provider 与 json_guard 的两类失败
（``LLM_UNAVAILABLE`` / ``LLM_SCHEMA_INVALID``）在这里被翻译成 State 的
``error_code`` / ``error_message`` —— 与 ``upload_file`` / ``rule_review``
同一套失败机制。**降级策略**（例如"LLM 失败就只用规则结果继续跑"）是
Conditional Edge 与后续步骤的裁决，本节点不替它决定。
"""

from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from app.core.errors import AgentErrorCode
from app.graph.context import ReviewContext
from app.graph.state import ContractReviewState
from app.llm.clause_review import review_clauses
from app.llm.findings import ClauseContext, ClauseReviewPromptInput, MatchedRuleHint
from app.llm.json_guard import LLMSchemaInvalidError
from app.llm.provider import LLMUnavailableError

logger = logging.getLogger(__name__)


async def llm_review(
    state: ContractReviewState,
    runtime: Runtime[ReviewContext],
) -> dict[str, object]:
    """让模型审一遍本批条款，把 findings 写回 State。

    * 读 State：``contract_type`` / ``clauses`` / ``rule_risks``
    * 写 State：``llm_findings``；失败时 ``error_code`` / ``error_message``

    **不抛业务异常**：两类 LLM 失败都翻译成明确的错误状态（见模块 docstring）。
    """
    provider = runtime.context.llm
    if provider is None:
        # 没有注入 provider = 这次运行不具备 LLM 审查能力。
        # 明确记录，而不是拿一个默认 provider 去悄悄发请求。
        message = "本次 Graph 运行没有注入 LLM provider（ReviewContext.llm 为空），未执行 LLM 审查"
        logger.warning("llm_review 缺少依赖 | %s", message)
        return {"error_code": AgentErrorCode.LLM_UNAVAILABLE.value, "error_message": message}

    contract_type = state.get("contract_type")
    if not contract_type:
        message = "缺少 Workflow 必需输入：contract_type（没有它无法确定审查视角）"
        logger.warning("llm_review 输入不完整 | %s", message)
        return {"error_code": AgentErrorCode.AGENT_INPUT_INVALID.value, "error_message": message}

    clauses = state.get("clauses") or []
    if not clauses:
        # 没有条款 = 文档里没有可审的内容（空文档），**不是"我们没跑"**。
        # 与规则侧同一口径：空文档下规则同样全部 NOT_MATCHED。
        logger.info("llm_review 跳过：文档没有可审条款 | file_id=%s", state.get("file_id"))
        return {"llm_findings": []}

    payload = _build_payload(state, contract_type=contract_type, clauses=clauses)

    try:
        outcome = await review_clauses(provider, payload)
    except LLMUnavailableError as exc:
        logger.warning("llm_review 调用失败（模型不可用）| file_id=%s %s", state.get("file_id"), exc)
        return {"error_code": exc.error_code, "error_message": str(exc)}
    except LLMSchemaInvalidError as exc:
        logger.warning("llm_review 输出不合契约 | file_id=%s %s", state.get("file_id"), exc)
        return {"error_code": exc.error_code, "error_message": str(exc)}

    logger.info(
        "llm_review 完成 | file_id=%s clauses=%d findings=%d model=%s tokens=%d/%d latency_ms=%d",
        state.get("file_id"),
        len(clauses),
        len(outcome.review.findings),
        outcome.llm.model,
        outcome.llm.prompt_tokens,
        outcome.llm.completion_tokens,
        outcome.llm.latency_ms,
    )
    return {"llm_findings": outcome.review.findings}


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _build_payload(
    state: ContractReviewState, *, contract_type: str, clauses: list
) -> ClauseReviewPromptInput:
    """把 State 里**已有的**东西组装成提示输入。

    ⚠️ 本轮**不做分批**：整份文档的条款一次性交出去。§9.2 的"按 clause_type 聚合、
    每批 ≤ 8 条且 ≤ 6000 字符"是后续步骤的事 —— 先让闭环成立，再谈切分。
    分批**不是**可以永远不做的优化：条款一多，单次提示会超长（那时再补）。

    规则命中转成 :class:`MatchedRuleHint`：``risk_code`` → ``rule_code``、
    ``risk_title`` → ``rule_name`` —— 字段名不同是因为两侧的说话方式不同
    （风险项说的是"我这条风险"，提示要说的是"哪条规则命中了"），**值原样搬运**。
    """
    return ClauseReviewPromptInput(
        contract_type=contract_type,
        clauses=[
            ClauseContext(
                clause_index=clause.clause_index,
                clause_type=clause.clause_type,
                clause_no=clause.clause_no,
                title=clause.title,
                text=clause.text,
            )
            for clause in clauses
        ],
        matched_rules=[
            MatchedRuleHint(
                rule_code=risk.risk_code,
                rule_name=risk.risk_title,
                dimension=risk.dimension,
                risk_level=risk.risk_level,
                quote=risk.quote,
            )
            for risk in (state.get("rule_risks") or [])
        ],
    )


__all__ = ["llm_review"]
