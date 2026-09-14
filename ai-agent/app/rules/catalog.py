"""规则目录 → Agent 规则快照（P8-2 第一小步）。

职责边界
-------
::

    Backend GET /api/v1/rule-sets            （P8-0，只读规则目录）
              │  BackendClient.get_effective_rule_set()   （HTTP，tools 层）
              ▼
        原始响应体 dict
              │  snapshot_from_backend()                  （本模块，纯函数）
              ▼
        RuleSetSnapshot ──▶ list[AgentRule]

**纯映射**：不发 HTTP、不碰 SQLAlchemy / FastAPI / LangGraph / LLM、不做文件 IO。
HTTP 那一半在 ``tools/backend_client.py``，本模块只回答"这份响应意味着什么规则"。

为什么与求值器分开
-----------------
"规则目录里有什么规则"与"一条规则是否命中"是两件事（P8-1 的立论）。
本模块负责前者，``evaluator.py`` 负责后者，中间隔着 :class:`AgentRule` ——
因此快照加载失败**不会**被误当成"某条规则没命中"。

Backend 的 ``rule_set.id`` 在这里被丢掉
------------------------------------
快照只保留 ``contract_type`` + ``rule_set_version`` + ``rules``。
``version`` 必须留下（它回答"这次审查依据的是哪一版规则"），
``id`` 不留（Agent 不需要数据库身份）。见 :func:`snapshot_from_backend`。

错误边界（谁抛什么）
-----------------
========================================  ====================================
连不上 / 超时 / HTTP 非 2xx / 2xx 但响应体   ``BackendRequestError``
不是 JSON **对象**                          （``tools`` 层的**传输**失败）
----------------------------------------  ------------------------------------
2xx + JSON 对象，但内容不符合规则集契约       ``RuleSnapshotError``
（缺 version / rules 不是数组 / 某条规则      （本层的**领域**失败）
转换失败 / 版本与规则自相矛盾）
========================================  ====================================

最后一行是刻意的分工：**"Backend 有没有给我一个 JSON 对象"是传输问题**，
由 ``BackendClient`` 判定，它因此不必认识规则领域；
**"这个 JSON 对象是不是一份可用的规则集"是领域问题**，由本模块判定。
``snapshot_from_backend`` 里仍有 ``Mapping`` 检查，那是给直接调用方的兜底
（经 ``BackendClient`` 的路径不会走到它）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.rules.schemas import AgentRule, RuleSetSnapshot, rule_from_backend


class RuleSnapshotError(Exception):
    """规则快照加载失败 —— Backend 的响应无法变成一份可用的 :class:`RuleSetSnapshot`。

    什么时候会抛
    ----------
    * 响应不是 JSON 对象 / ``rules`` 不是数组 / ``rule_set`` 存在但缺 ``version``
    * 任何一条规则过不了 :func:`rule_from_backend`（缺必填字段、字段类型不对……）

    **整份快照一起失败，不做"跳过坏规则、保留好规则"**
    ------------------------------------------------
    部分可用的快照意味着审查时**少跑了几条规则**，而结果看起来完全正常 ——
    这正是 P8-1 一直在挡的那种静默漏报（把"没跑"说成"没问题"）。
    宁可整份不可用，也不给一份"看起来能跑"的残缺规则集。

    为什么包一层而不是直接把 ``pydantic.ValidationError`` 扔出去
    -----------------------------------------------------------
    调用方（P8-2 的节点）需要的是"这份快照不可用"这一个判断，
    不该为此去认识 Pydantic 的异常结构；而且包一层才能补上
    **是哪条规则坏了**这个 Pydantic 报不出来（或很难读）的信息。

    ⚠️ 异常本身不会让进程崩溃：接住它并翻译成 State 是 P8-2 节点的事。
    这里只负责把"为什么不可用"说清楚。
    """


def snapshot_from_backend(payload: Mapping[str, Any]) -> RuleSetSnapshot:
    """把 Backend ``EffectiveRuleSetResponse`` 的字典映射成 :class:`RuleSetSnapshot`。

    这是两侧**规则集契约的唯一转换路径**；每条规则内部仍走
    :func:`~app.rules.schemas.rule_from_backend`（规则契约的唯一转换路径）。

    与 Backend 真实响应形状的对应
    --------------------------
    ==========================================  ==================================
    Backend ``{contract_type, rule_set, rules}`` Agent ``RuleSetSnapshot``
    ==========================================  ==================================
    ``contract_type``                           ``contract_type``
    ``rule_set.version``                        ``rule_set_version``
    ``rule_set`` 为 ``null``                     ``rule_set_version=None``（不是错误）
    ``rule_set.id`` / ``name`` / ``description``  **丢弃**
    ``rules[]``                                 ``rules[]``（顺序原样保留）
    ==========================================  ==================================

    ``rule_set`` 为 ``null`` 但 ``rules`` 非空是**坏响应**，不是"没有规则集"
    ---------------------------------------------------------------
    这种组合自相矛盾（没有规则集，却带着规则）。它不会在这里被悄悄抹平 ——
    构造 :class:`RuleSetSnapshot` 时领域不变量会拒绝它，进而抛出
    :class:`RuleSnapshotError`。理由见该模型的不变量说明。

    :raises RuleSnapshotError: 响应结构不符合契约，或有任意一条规则无法转换
    """
    if not isinstance(payload, Mapping):
        raise RuleSnapshotError(f"规则集响应不是 JSON 对象：{type(payload).__name__}")

    raw_rule_set = payload.get("rule_set")
    raw_rules = payload.get("rules")

    if raw_rule_set is not None and not isinstance(raw_rule_set, Mapping):
        raise RuleSnapshotError(f"rule_set 既不是 null 也不是对象：{type(raw_rule_set).__name__}")
    if not isinstance(raw_rules, list):
        raise RuleSnapshotError(f"rules 不是数组：{type(raw_rules).__name__}")

    rule_set_version = _version_of(raw_rule_set)

    try:
        return RuleSetSnapshot(
            contract_type=payload.get("contract_type"),
            rule_set_version=rule_set_version,
            # 顺序**原样保留** —— Backend 已经按 sort_order ASC, id ASC 排好，
            # Agent 不重排（重排就是在两个服务里各存一份排序规则）。
            rules=[_agent_rule(item) for item in raw_rules],
        )
    except ValidationError as exc:
        raise RuleSnapshotError(f"规则集响应不符合契约：{exc}") from exc


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _version_of(raw_rule_set: Mapping[str, Any] | None) -> str | None:
    """取规则集版本；``rule_set=null`` 时返回 ``None``（= 没有启用的规则集）。"""
    if raw_rule_set is None:
        return None

    version = raw_rule_set.get("version")
    if not isinstance(version, str) or not version:
        # 不能静默当成 None —— 那会把"响应坏了"伪装成"这个合同类型没有规则集"，
        # 而后者是一个**正常运行**的结论。
        raise RuleSnapshotError("rule_set 存在但 version 缺失或不是非空字符串，规则版本无法确定")
    return version


def _agent_rule(payload: Any) -> AgentRule:
    """单条规则 → :class:`AgentRule`（**必须**经 ``rule_from_backend``）。"""
    if not isinstance(payload, Mapping):
        raise RuleSnapshotError(f"rules 的元素不是对象：{type(payload).__name__}")

    try:
        return rule_from_backend(payload)
    except ValidationError as exc:
        rule_code = payload.get("rule_code") or "<缺少 rule_code>"
        raise RuleSnapshotError(f"规则 {rule_code} 无法转换成 AgentRule：{exc}") from exc


__all__ = ["RuleSnapshotError", "snapshot_from_backend"]
