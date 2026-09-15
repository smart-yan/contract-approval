"""Agent 的「风险持久化」业务动作（P9-10）。

它是**边界层**：一边是 Agent 的领域模型，另一边是 Backend 的持久化契约，
字段映射只在这里发生一次。

::

    AgentRiskItem（Agent 领域模型）
              │  本模块的 _to_payload —— **唯一的映射点**
              ▼
    RiskItemCreate（Backend 的请求契约）
              │  BackendClient.persist_task_risks
              ▼
    risk_item（Backend 持久化模型）

为什么映射必须有单独一层
----------------------
两个模型的字段**同名不同义**，直接对拷会安静地写错数据：

==================================  ==================================================
``AgentRiskItem.original_text``     **证据所在的段落原文**
``risk_item.original_text``         **命中的原文片段**
==================================  ==================================================

所以这一层存在的首要理由不是"翻译字段名"，而是**阻止按名字对拷**：
Agent 的 ``quote``（已经过 P9-4 定位核对、保证是 ``original_text`` 的子串）才是
Backend 那一列要的东西。Agent 的段落原文**不落库**（P9-10 裁决：不新增字段）。

不传的字段
---------
``risk_item`` 里还有一批字段**由 Backend 服务端决定**，Agent 传了也没用
（请求契约里根本没有这些键）：``review_status``（固定 PENDING）、
``locator_type``（按附件类型派生）、``rule_id`` / ``clause_id``（由 Backend 解析）、
``task_id`` / ``contract_id``（来自 URL 与任务自身）。本模块**不构造**它们 ——
"Agent 不该知道 Backend 的主键"这句话，靠的就是这里没有这些键。

``related_rule_code`` 在 ``risk_item`` 里**没有对应列**：P9-10 裁决接受丢弃 ——
合并已经发生过，它的使命到此结束；合并结果由 ``source=RULE+LLM`` 与
``risk_code`` 表达。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.risk.schemas import AgentRiskItem
from app.tools.backend_client import BackendClient


@dataclass(frozen=True, slots=True)
class RiskPersistenceRequest:
    """一次风险持久化所需的输入。"""

    task_id: int
    risks: Sequence[AgentRiskItem] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RiskPersistenceResult:
    """风险持久化的输出契约。

    ``ok=False`` 时 ``error_code`` / ``error_message`` 有值 ——
    ``error_code`` 可能是 Agent 侧的错误码（连不上 / 响应不是 JSON），
    也可能是 Backend 自己的错误码（``TASK_ALREADY_PERSISTED`` / ``RULE_NOT_FOUND`` /
    ``VALIDATION_ERROR``）**原样带回**：翻译成 Agent 的错误码会丢掉"到底是谁拒绝的、
    为什么"这一层信息。
    """

    ok: bool
    persisted: int | None = None
    task_status: str | None = None
    task_stage: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class RiskPersistenceTool:
    """Agent 的「把最终风险写回 Backend」业务动作。

    :param backend: HTTP 通信层。Tool 只依赖它的抽象行为 ——
        测试里换一个挂 ``httpx.MockTransport`` 的实例就能跑。
    """

    def __init__(self, backend: BackendClient) -> None:
        self._backend = backend

    async def run(self, request: RiskPersistenceRequest) -> RiskPersistenceResult:
        """把整批风险提交给 Backend。

        本方法**不抛业务异常**：所有失败都翻译成 ``ok=False`` 的结果，
        由调用它的节点写进 State。

        **空列表也会发请求**：``risks=[]`` 是一次真实结论（"这次审查没有风险"），
        Backend 会照常把任务置为已完成。跳过这次调用会让任务永远停在 pending，
        而"没有风险"与"我们没跑"就再也分不出来了。
        """
        outcome = await self._backend.persist_task_risks(
            request.task_id,
            risks=[_to_payload(risk) for risk in request.risks],
        )

        if not outcome.ok or outcome.payload is None:
            return RiskPersistenceResult(
                ok=False,
                error_code=outcome.error_code,
                error_message=outcome.error_message,
            )

        payload = outcome.payload
        return RiskPersistenceResult(
            ok=True,
            persisted=_as_int(payload.get("persisted")),
            task_status=_as_str(payload.get("task_status")),
            task_stage=_as_str(payload.get("task_stage")),
        )


# --------------------------------------------------------------------------- #
# 字段映射：AgentRiskItem → Backend 请求条目
# --------------------------------------------------------------------------- #
def _to_payload(risk: AgentRiskItem) -> dict[str, Any]:
    """把一条统一风险项映射成 Backend 的 ``RiskItemCreate``。

    ⚠️ ``quote`` 而不是 ``original_text`` —— 见模块 docstring。
    这一行是本模块最容易被"顺手改对"的地方，因此 ``tests/unit/test_risk_persistence_tool.py``
    里有一条专门断言"Agent 的段落原文**没有**出现在请求里"的用例。
    """
    return {
        "risk_code": risk.risk_code,
        "risk_title": risk.risk_title,
        "dimension": risk.dimension,
        "risk_level": risk.risk_level,
        "source": risk.source.value,
        "reason": risk.reason,
        "legal_basis": risk.legal_basis,
        "quote": risk.quote,
        "paragraph_index": risk.paragraph_index,
        "anchor_method": risk.anchor_method,
    }


# --------------------------------------------------------------------------- #
# 响应字段的防御性取值（与 contract_ingest 同一口径）
# --------------------------------------------------------------------------- #
def _as_int(value: object) -> int | None:
    # bool 是 int 的子类，必须排除，否则 True 会被当成合法计数
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "RiskPersistenceRequest",
    "RiskPersistenceResult",
    "RiskPersistenceTool",
]
