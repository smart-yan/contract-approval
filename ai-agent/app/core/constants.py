"""Agent 侧的字符串契约常量。

⚠️ 为什么这里要**复制** Backend 的枚举，而不是共享一个包
------------------------------------------------------
``ClauseType`` / ``ExtractMethod`` 的取值同时存在于两个服务：
Backend 用它们建表存数据，Agent 用它们产出数据。

把 Backend 的常量抽成共享包，会让**已经通过 HTTP 解耦的两个服务重新绑死**
（版本、发布节奏、部署耦合）—— 为了省几个字符串而付出这个代价不划算。
枚举取值本身就是一份契约，契约两端各有一份声明是正常的
（就像 protobuf 两端各有生成代码）。

**代价与兜底**：两份声明可能漂移。Agent 侧用 ``test_understanding_contract.py``
把取值集合钉死，能挡住"Agent 侧被误改"；**挡不住"Backend 改了而 Agent 没跟"** ——
真正的跨服务漂移检查需要共享包或跨仓测试，两者都超出当前范围。这是已知缺口。
"""

from __future__ import annotations

from enum import StrEnum


class ClauseType(StrEnum):
    """条款类型。

    ⚠️ **必须与 ``backend/app/core/constants.py`` 的 ``ClauseType`` 保持一致。**
    改动需两侧同步。
    """

    SUBJECT = "SUBJECT"  # 主体
    AMOUNT_PAYMENT = "AMOUNT_PAYMENT"  # 金额支付
    ACCEPTANCE = "ACCEPTANCE"  # 验收
    LIABILITY = "LIABILITY"  # 违约责任
    CONFIDENTIAL = "CONFIDENTIAL"  # 保密
    IP = "IP"  # 知识产权
    DISPUTE = "DISPUTE"  # 争议管辖
    FORCE_MAJEURE = "FORCE_MAJEURE"  # 不可抗力
    DATA_SECURITY = "DATA_SECURITY"  # 数据安全
    DELIVERY = "DELIVERY"  # 交付
    OTHER = "OTHER"


class RuleType(StrEnum):
    """规则求值器类型（P8-1 引入）。

    ⚠️ **必须与 ``backend/app/core/constants.py`` 的 ``RuleType`` 保持一致。**

    为什么 Agent 侧需要它：``AgentRule.rule_type`` 是**字符串**（Backend 的规则
    目录可配置，收成枚举会让一条配置错的规则拖垮整份规则集），求值器拿这个枚举
    做**分派**——两者分工不同：``str`` 负责"装得下任何值"，``StrEnum`` 负责
    "我们认识哪几种"。因此 :class:`~app.rules.schemas.AgentRule` 里的
    ``rule_type`` 是 ``str``，求值器里的比较对象是这里的成员。

    ``StrEnum`` 成员是 ``str`` 子类，``"KEYWORD" == RuleType.KEYWORD`` 为真，
    所以分派不需要把输入解析成枚举，**不认识的值自然落到"求值失败"分支**。
    """

    KEYWORD = "KEYWORD"  # 关键词命中
    REGEX = "REGEX"  # 正则匹配
    EXISTS = "EXISTS"  # 该条款必须存在
    MISSING = "MISSING"  # 必备条款缺失
    THRESHOLD = "THRESHOLD"  # 数值比较


class ExtractMethod(StrEnum):
    """提取/切分方式。

    ⚠️ **必须与 ``backend/app/core/constants.py`` 的 ``ExtractMethod`` 保持一致。**

    P7-1 的条款切分统一使用 :attr:`RULE` —— 它是"正则 + 编号模式选择 + 区间推导"
    的组合，不是单条正则（``REGEX`` 在 Backend 侧被标注为**元数据字段专用**）。
    ``LLM`` 留给将来"无编号文档靠语义分段"的兜底。
    """

    RULE = "RULE"  # 正则 / 规则
    REGEX = "REGEX"  # 正则（元数据字段使用此值）
    LLM = "LLM"  # 大模型兜底
    MANUAL = "MANUAL"  # 人工录入 / 修正


__all__ = ["ClauseType", "ExtractMethod", "RuleType"]
