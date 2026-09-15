"""审查风险持久化的对外契约（P9-10）。

它解决什么
---------
Agent 跑完 ``merge_risks`` 之后手里有一份 ``AgentRiskItem`` 列表，
但 **State 不落库、进程一结束就没了**。本契约是那条"把最终风险结果可靠写进
Backend"的通道。

为什么请求里**没有**这些字段
--------------------------
下面每一个都**刻意不接受客户端指定** —— 它们要么是 Backend 的事实，
要么是必须由服务端统一决定的字段。让客户端传，等于把"谁来保证一致性"
从服务端挪到了调用方：

========================  ================================================
``review_status``         人工复核状态。服务端固定 ``PENDING``。
                          **若允许客户端指定，它就能伪造「已确认」**
``locator_type``          由所属附件类型派生（DOCX→PARAGRAPH、PDF→PAGE），
                          与 ``document_block.locator_type`` 同源（§10.4）
``rule_id`` / ``clause_id`` 数据库主键。Agent 不认识它们，也不应该认识
                          （那会让 Backend 的主键渗进 Agent 的契约）
``task_id`` / ``contract_id`` 来自 URL 路径与任务自身，不由请求体重复声明
========================  ================================================

⚠️ ``quote`` 而不是 ``original_text``
-----------------------------------
请求里的 ``quote`` 对应 ``risk_item.original_text`` 列（"命中的原文片段"）。
**字段名刻意不叫 ``original_text``**：在 Agent 的 ``AgentRiskItem`` 里，
``original_text`` 指的是**证据所在的段落原文**，与这一列的语义**正好不同**。
沿用同一个名字会让"按名字对拷"变成一个看起来无害、实际上把整段原文写进
"命中片段"列的错误 —— 那是人工核对时最直接的证据丢失。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.constants import AnchorMethod, RiskLevel, RiskSource


class RiskItemCreate(BaseModel):
    """一条待持久化的风险（Agent 的 ``AgentRiskItem`` 在边界上的形状）。"""

    risk_code: str | None = Field(
        default=None,
        max_length=64,
        description="规则编码。**有值时必须能在该任务的规则集里解析到对应规则**，"
        "否则整批拒绝；纯 LLM 风险为空（此时 rule_id 也为空）",
    )
    risk_title: str = Field(min_length=1, max_length=255, description="风险项名称")
    dimension: str = Field(min_length=1, max_length=32, description="审查维度")
    risk_level: RiskLevel = Field(description="风险等级，取值见 constants.RiskLevel")
    source: RiskSource = Field(description="风险来源，取值见 constants.RiskSource")
    reason: str | None = Field(default=None, description="风险成因分析")
    legal_basis: str | None = Field(default=None, description="法律合规依据")
    quote: str = Field(
        min_length=1,
        description="**命中原文的逐字片段**。写入 ``risk_item.original_text`` 列。"
        "⚠️ 不要传整段原文 —— 这一列的语义是片段，人工核对的依据就是它",
    )
    paragraph_index: int = Field(ge=0, description="段落索引。服务端据此解析 ``clause_id``")
    anchor_method: AnchorMethod | None = Field(
        default=None,
        description="坐标反查方式（§10.5）。规则来源为空；模型来源为定位器的产出",
    )


class RiskPersistRequest(BaseModel):
    """一次"把这批风险写入该任务"的请求。

    整批**原子**：任何一条校验不过，一条都不会落库（见
    ``services/risk_persistence.py`` 的事务边界说明）。
    """

    risks: list[RiskItemCreate] = Field(
        default_factory=list,
        description="该任务的全部风险。允许为空列表（一份没有风险也是结论），"
        "但**不允许对已有风险的任务再写一次**",
    )


class RiskPersistResponse(BaseModel):
    """持久化结果。"""

    task_id: int = Field(description="审查任务 ID")
    persisted: int = Field(description="本次写入的风险条数")
    task_status: str = Field(description="更新后的任务状态，取值见 constants.TaskStatus")
    task_stage: str = Field(description="更新后的任务阶段，取值见 constants.TaskStage")


__all__ = ["RiskItemCreate", "RiskPersistRequest", "RiskPersistResponse"]
