"""``llm_review`` 节点 —— 编排"让模型审一遍条款，并把它的说法落到文档上"（P9-5 / P9-7）。

调用链
-----
::

    llm_review Node                →  llm.clause_review.review_clauses()
    （State 编解码 + 组装输入）        （业务层：提示 + Provider + JSON Guard）
                                   →  llm.finding_resolution.resolve_findings()
                                      （业务层：定位 + related_rule_code 核对）

节点**只做 State 编解码**，与 ``identify_clauses`` / ``rule_review`` 同构：

* 从 State 里取出**已有的**条款、规则命中、段落序列与规则快照，组装输入
* 调用业务层的两个纯函数式能力（审查调用 + 落地解析）
* 把结果写进 ``llm_findings``
* 把失败翻译成明确的 State 状态

它**不**自己拼提示、不自己拼 JSON Schema、不碰 HTTP、不解析 JSON、
**也不自己搜 quote** —— 那些分别是 ``prompts`` / ``json_guard`` / ``provider`` /
``understanding.locator`` 的职责。节点里若出现第二套提示、第二份 schema
或第二个定位实现，就会开始各自漂移。

``llm_findings`` 里装的是**已定位**的发现（P9-7 起）
-------------------------------------------------
元素是 :class:`~app.llm.finding_resolution.ResolvedFinding`：
模型的说法（``.finding``）**加上** Agent 核出来的位置
（``paragraph_index`` / ``original_text`` / ``quote`` / ``anchor_method``）。

字段名仍然叫 ``llm_findings``（本步**不改名**），但它的语义已经推进了一步：
不再是"模型说了什么"，而是"模型说了什么，且我们能指出它在文档的哪一段"。
⚠️ 它**仍然不是风险项** —— 没有 ``source``、没有合并、没有等级加工，
那些是后续步骤的事。

失败语义（P9-6a 起：LLM 失败是**降级**，不是整次审查失败）
------------------------------------------------------
**不吞异常、也不让它冒泡炸掉整张图**：Provider 与 json_guard 的两类失败
（``LLM_UNAVAILABLE`` / ``LLM_SCHEMA_INVALID``）在这里被翻译成
``llm_error_code`` / ``llm_error_message`` —— 一条**独立的降级通道**。

为什么不写 ``error_code``：§9.1 第 4 道防线要求"本批降级为**仅规则引擎结果**并标记
warning，绝不让整个任务失败"。LLM 挂了的时候规则结果仍然完整可用，
借用整次审查的失败通道会让 API 把这种情形报成 rejected —— 一次"规则部分照常可用"
的审查被说成失败，既不准确，也会让人以为整个任务白跑了。
两条通道各有各的分流（见 ``route_after_llm_review``）。

唯一的**致命**失败仍然是输入缺失（``contract_type`` 或 ``parse_result``）：
那种情况下这个节点根本组不出提示、也没有段落可定位，
而且 ``upload_file`` / ``parse_document`` 早就该拦住了。
"""

from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from app.core.errors import AgentErrorCode
from app.graph.context import ReviewContext
from app.graph.state import ContractReviewState
from app.llm.clause_review import review_clauses
from app.llm.finding_resolution import resolve_findings
from app.llm.findings import ClauseContext, ClauseReviewPromptInput, MatchedRuleHint
from app.llm.json_guard import LLMSchemaInvalidError
from app.llm.provider import LLMUnavailableError
from app.schemas.document import Paragraph
from app.understanding.locator import ANCHOR_CLAUSE_FALLBACK

logger = logging.getLogger(__name__)


async def llm_review(
    state: ContractReviewState,
    runtime: Runtime[ReviewContext],
) -> dict[str, object]:
    """让模型审一遍本批条款，把 findings **落到文档上**后写回 State。

    * 读 State：``contract_type`` / ``clauses`` / ``parse_result`` / ``rule_risks`` /
      ``rule_snapshot``
    * 写 State：``llm_findings``（已定位的发现）；失败时走降级通道
      ``llm_error_code`` / ``llm_error_message``

    **不抛业务异常**：两类 LLM 失败都翻译成明确的错误状态（见模块 docstring）。
    """
    provider = runtime.context.llm
    if provider is None:
        # 没有注入 provider = 这次运行不具备 LLM 审查能力。**这是降级，不是失败**：
        # 规则结果已经产出且仍然可用，整次审查不该因此被判失败。
        # 明确记录到降级通道，而不是拿一个默认 provider 去悄悄发请求。
        message = "本次 Graph 运行没有注入 LLM provider（ReviewContext.llm 为空），未执行 LLM 审查"
        logger.warning("llm_review 降级 | %s", message)
        return {"llm_error_code": AgentErrorCode.LLM_UNAVAILABLE.value, "llm_error_message": message}

    missing = [name for name in ("contract_type", "parse_result") if not state.get(name)]
    if missing:
        # 没有这些就既组不出提示、也没有段落可定位 —— 真正的输入缺失
        message = f"缺少 Workflow 必需输入：{', '.join(missing)}"
        logger.warning("llm_review 输入不完整 | %s", message)
        return {"error_code": AgentErrorCode.AGENT_INPUT_INVALID.value, "error_message": message}

    contract_type = state["contract_type"]

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
        logger.warning("llm_review 降级（模型不可用）| file_id=%s %s", state.get("file_id"), exc)
        return {"llm_error_code": exc.error_code, "llm_error_message": str(exc)}
    except LLMSchemaInvalidError as exc:
        logger.warning("llm_review 降级（输出不合契约）| file_id=%s %s", state.get("file_id"), exc)
        return {"llm_error_code": exc.error_code, "llm_error_message": str(exc)}

    # ---- 落地解析：模型的说法 → 文档里**真实存在**的位置（P9-7 接入）----
    # 复用 P9-4 的 resolve_findings（节点不自己搜 quote）：
    #   clause_index 站不住 → 丢弃那一条；quote 定位不唯一 → 降级到条款级；
    #   related_rule_code 核不上 → 清空关联。三条规则各自写 warning（在 resolver 里）。
    resolved = resolve_findings(
        findings=outcome.review.findings,
        clauses=clauses,
        paragraphs=_paragraphs_of(state),
        known_rule_codes=_known_rule_codes(state),
    )

    fallback_count = sum(1 for item in resolved if item.anchor_method == ANCHOR_CLAUSE_FALLBACK)
    logger.info(
        "llm_review 完成 | file_id=%s clauses=%d findings=%d resolved=%d dropped=%d fallback=%d "
        "model=%s tokens=%d/%d latency_ms=%d",
        state.get("file_id"),
        len(clauses),
        len(outcome.review.findings),
        len(resolved),
        len(outcome.review.findings) - len(resolved),
        fallback_count,
        outcome.llm.model,
        outcome.llm.prompt_tokens,
        outcome.llm.completion_tokens,
        outcome.llm.latency_ms,
    )
    return {"llm_findings": resolved}


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _paragraphs_of(state: ContractReviewState) -> list[Paragraph]:
    """定位要用的段落序列。

    ``ParseResult.paragraphs`` 是 P6-2 的位置契约本体 —— 段落序号只有回到它身上
    才能变成"真实的第几段"。解析没跑过时上面那道必需输入守卫已经拦住了，
    这里只是把它取出来（``clauses`` 与它同源，本来就分不开）。
    """
    parse_result = state.get("parse_result")
    return parse_result.paragraphs if parse_result is not None else []


def _known_rule_codes(state: ContractReviewState) -> set[str]:
    """本次审查所用规则快照里的全部 ``rule_code``（供核对 ``related_rule_code``）。

    快照缺失（例如上游 ``rule_review`` 已经失败）时返回**空集合** ——
    于是一个关联都不会被接受（P9-4 的保守口径：没有依据的关联一律清空）。
    这里**不报错**：LLM 审查本身仍然可以照常进行，只是不建立任何规则关联。
    """
    snapshot = state.get("rule_snapshot")
    return {rule.rule_code for rule in snapshot.rules} if snapshot is not None else set()


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
