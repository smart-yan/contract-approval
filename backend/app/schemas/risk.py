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

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.constants import AnchorMethod, RiskLevel, RiskReviewStatus, RiskSource


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


# =========================================================================== #
# 人工复核（P13-1）
# =========================================================================== #
class RiskReviewRequest(BaseModel):
    """法务对**一条**风险的复核结论。

    只改人工判断，**不改 AI 的判断内容**
    ------------------------------------
    请求体里能出现的字段只有下面三个。``risk_title`` / ``reason`` / ``legal_basis`` /
    ``original_text`` / ``paragraph_index`` / ``clause_id`` / ``rule_id`` / ``task_id`` /
    ``contract_id`` …… 一律**不在契约里**，而且 ``extra="forbid"`` 让它们连传都传不进来
    （见下方"为什么是 forbid 而不是忽略"）。

    为什么 ``MODIFIED`` 必须带 ``risk_level``
    ----------------------------------------
    ``MODIFIED`` 的定义就是"法务调整了等级"（§6.3）。不带新等级的 ``MODIFIED``
    与 ``CONFIRMED`` 无法区分，落库后谁也说不清"改了没有、改成什么"。

    为什么 ``CONFIRMED`` / ``REJECTED`` **不能**带 ``risk_level``
    ------------------------------------------------------------
    这两种结论的语义是"AI 判的没错"与"AI 判错了"，它们**不包含**等级修订。
    允许顺带改等级，等于给"只想确认一下"的调用方留了一条静默改写等级的路径 ——
    而等级直接决定报告里的风险分布，且**不会报错**。

    为什么是 ``forbid`` 而不是默认的忽略
    ----------------------------------
    Pydantic 默认忽略未知字段。那意味着调用方发一个 ``risk_title`` 会拿到 **200**，
    然后以为标题改成功了 —— 实际上服务端一个字都没动。``forbid`` 让这类误用
    在第一次调用时就以 422 暴露出来。
    """

    model_config = ConfigDict(extra="forbid")

    review_status: RiskReviewStatus = Field(
        description="复核结论，取值见 constants.RiskReviewStatus。"
        "**不接受 `PENDING`** —— 它是 AI 产出时的初始状态，把一条已复核的风险"
        "改回 `PENDING` 等于抹掉复核痕迹，而这个接口没有历史记录可查"
    )
    review_comment: str | None = Field(default=None, description="复核意见（可选）")
    risk_level: RiskLevel | None = Field(
        default=None,
        description="人工修订后的风险等级。**仅当 `review_status = MODIFIED` 时必填**；"
        "`CONFIRMED` / `REJECTED` 携带它会被拒绝",
    )

    @model_validator(mode="after")
    def _check_rules(self) -> RiskReviewRequest:
        if self.review_status is RiskReviewStatus.PENDING:
            raise ValueError(
                "review_status 不能是 PENDING：PENDING 是 AI 产出时的初始状态，"
                "把它作为复核结果等于抹掉复核痕迹"
            )

        if self.review_status is RiskReviewStatus.MODIFIED:
            if self.risk_level is None:
                raise ValueError("review_status 为 MODIFIED 时必须提供新的 risk_level")
        elif self.risk_level is not None:
            raise ValueError(
                f"review_status 为 {self.review_status.value} 时不允许修改 risk_level —— "
                "只有 MODIFIED 才表示法务调整了等级"
            )

        return self


class RiskReviewResponse(BaseModel):
    """复核写入后，该风险项的复核相关状态（即库里的实际值）。"""

    risk_id: int = Field(description="风险项 ID")
    task_id: int = Field(description="所属审查任务 ID")
    risk_level: str = Field(description="当前风险等级（MODIFIED 时是人工修订后的值）")
    review_status: str = Field(description="复核状态，取值见 constants.RiskReviewStatus")
    review_comment: str | None = Field(default=None, description="复核意见")
    reviewer_id: int | None = Field(
        default=None,
        description="复核人。⚠️ **当前恒为 null** —— 项目没有 `sys_user` 表，"
        "也没有登录/JWT/RBAC（§15 明确不做）。服务端**不伪造**一个用户 id 来填这一列",
    )
    reviewed_at: datetime = Field(
        description="复核时刻。**服务端生成的 naive UTC**（见 app.utils.datetime_utils），"
        "不接受客户端指定"
    )


__all__ = [
    "RiskItemCreate",
    "RiskPersistRequest",
    "RiskPersistResponse",
    "RiskReviewRequest",
    "RiskReviewResponse",
]
