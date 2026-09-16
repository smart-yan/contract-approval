"""全项目共享枚举词汇。

架构文档：§2.1 core/constants.py（枚举：任务状态/回写状态/风险等级/条款类型...）。

本模块**只有枚举声明，不含任何业务逻辑**，是 P3 建表、P6 状态机、P7 解析
共同依赖的「词汇表」。每个枚举都标注了架构文档出处，便于回溯与评审。

范围纪律
--------
只登记**当前阶段或近期基础设施已有实际调用方**的枚举。远期模块的枚举在进入对应
阶段时按需追加 —— 避免"声明先行、无人使用"的维护噪声。

P3 恢复的 6 个枚举（``RuleType`` / ``UserRole`` / ``SuggestionType`` /
``ContractSource`` / ``AnchorMethod`` / ``LLMScene``）：
它们在 P2-b 曾因"没有任何调用方"被收缩掉；P3 建立 ORM 模型后，这些枚举对应的
**表列已经真实落地**（如 ``review_rule.rule_type``、``contract.source``），
枚举重新获得了明确的调用方（模型字段的类型注解），因此按预定的"进入对应阶段时
按需追加"原则恢复。

> ⚠️ 恢复的**只是类型定义**。枚举背后的业务逻辑（规则引擎、认证、回写、LLM 调用）
> 仍然未实现，分别属于 P9 / P4 / P12 / P10。

实现约定
--------
* 统一使用 ``StrEnum``（Python 3.11+）：成员本身即字符串，
  既是 ``str`` 子类可直接比较/序列化，又保留枚举的类型检查。
* 数据库中以 VARCHAR 存储其字面值（架构文档 §7.2 明确各字段为 VARCHAR），
  不用 MySQL 原生 ENUM 类型 —— 避免加值时必须改表结构。
* 枚举值的字面量**一经确定不再修改**（已落库的数据依赖它），只允许新增。
"""

from __future__ import annotations

from enum import StrEnum

# =========================================================================== #
# 一、任务状态机（架构文档 §6.1）
# =========================================================================== #


class TaskStatus(StrEnum):
    """审查任务状态。§6.1：pending → parsing → reviewing → completed，异常进 blocked。"""

    PENDING = "pending"
    PARSING = "parsing"
    REVIEWING = "reviewing"
    BLOCKED = "blocked"
    COMPLETED = "completed"


#: §6.1 合法迁移矩阵（唯一权威）。键为 from，值为允许到达的 to 集合。
#: 未出现在此表中的组合一律非法；``completed`` 为终态，重新审查须新建任务。
TASK_STATUS_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.PARSING, TaskStatus.BLOCKED}),
    TaskStatus.PARSING: frozenset({TaskStatus.PENDING, TaskStatus.REVIEWING, TaskStatus.BLOCKED}),
    TaskStatus.REVIEWING: frozenset(
        {TaskStatus.PENDING, TaskStatus.REVIEWING, TaskStatus.BLOCKED, TaskStatus.COMPLETED}
    ),
    TaskStatus.BLOCKED: frozenset({TaskStatus.PENDING}),
    TaskStatus.COMPLETED: frozenset(),  # 终态，不允许任何迁出
}


class TaskStage(StrEnum):
    """阶段级断点标记，用于断点续跑（§6.1）。

    重跑时按 ``contract_id + current_stage`` 判断产物是否已存在，已完成的阶段直接跳过
    （§1.4 硬规则 6）。
    """

    UPLOADED = "UPLOADED"
    PARSED = "PARSED"
    CLAUSED = "CLAUSED"
    REVIEWED = "REVIEWED"


class BlockReasonCode(StrEnum):
    """任务阻塞原因（§6.1）。

    枚举化的价值：可统计、可自愈、可让前端按 code 给出针对性的修复引导，
    而不是把一段人话字符串塞给用户。人话原因另存 ``block_reason_msg``。
    """

    FILE_ENCRYPTED = "FILE_ENCRYPTED"  # 文档加密
    FILE_CORRUPTED = "FILE_CORRUPTED"  # 文件损坏
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"  # 格式不支持
    EMPTY_TEXT = "EMPTY_TEXT"  # 正文为空
    OCR_FAILED = "OCR_FAILED"  # OCR 执行失败
    OCR_LOW_QUALITY = "OCR_LOW_QUALITY"  # 扫描件严重模糊，置信度过低
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"  # 模型服务不可用
    LLM_SCHEMA_INVALID = "LLM_SCHEMA_INVALID"  # 模型输出始终无法通过结构校验
    TIMEOUT = "TIMEOUT"  # 任务超时
    INTERNAL_ERROR = "INTERNAL_ERROR"  # 其它内部错误
    #: Agent 在**后台**执行整张图时异常退出（P14-4）。
    #: ⚠️ 与 ``INTERNAL_ERROR`` 分开：那个是 Backend 自己的内部错误，
    #: 这个的责任方在 Agent（后台任务崩了，Backend 侧只是被通知）。
    #: 混在一起排查时分不清该看哪边的日志 —— 与 ``DOCUMENT_MAPPING_INVALID``
    #: 区分 ``BACKEND_REJECTED`` 是同一条理由。
    AGENT_GRAPH_EXECUTION_FAILED = "AGENT_GRAPH_EXECUTION_FAILED"


class ParseStatus(StrEnum):
    """单文件解析状态（§7.2 ``contract_file.parse_status``）。

    P4（合同文件接入）上传成功后**只设置** ``PENDING``；
    实际的解析状态流转由 **P7**（文档解析引擎）负责，本阶段不实现流转逻辑。
    """

    PENDING = "PENDING"  # 已接入，等待解析
    PARSING = "PARSING"  # 解析中
    PARSED = "PARSED"  # 解析完成
    FAILED = "FAILED"  # 解析失败


# =========================================================================== #
# 二、回写状态机（架构文档 §6.2）
# =========================================================================== #


class WritebackStatus(StrEnum):
    """审批意见回写状态。§6.2：not_written → writing → success / failed。"""

    NOT_WRITTEN = "not_written"
    WRITING = "writing"
    SUCCESS = "success"
    FAILED = "failed"


#: §6.2 合法迁移矩阵。``success`` 为终态且受幂等键保护，不允许再次写回。
WRITEBACK_STATUS_TRANSITIONS: dict[WritebackStatus, frozenset[WritebackStatus]] = {
    WritebackStatus.NOT_WRITTEN: frozenset({WritebackStatus.WRITING}),
    WritebackStatus.WRITING: frozenset({WritebackStatus.SUCCESS, WritebackStatus.FAILED}),
    WritebackStatus.FAILED: frozenset({WritebackStatus.WRITING}),
    WritebackStatus.SUCCESS: frozenset(),
}


# =========================================================================== #
# 三、风险等级与审查结论（架构文档 §11.2）
# =========================================================================== #


class RiskLevel(StrEnum):
    """**单条**风险的风险等级（§11.2）。

    ⚠️ 它是 ``risk_item.risk_level`` 的取值，**不是**"综合等级"。
    综合等级（§11.2 的评分结果，落到 ``review_task.risk_level_final``）归 Agent，
    当前尚未实现，恒为 ``NULL`` —— Backend **不**根据它算综合结论，
    也**不**因为人工复核而排除 ``REJECTED`` 重算。详见 ``RiskReviewStatus``。
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ReviewConclusion(StrEnum):
    """审查结论（§11.2）—— **词表已定义，但当前没有任何代码产生它**。

    ⚠️ 它是 §11.2 评分（归 Agent）的产物，落到 ``review_task.conclusion``，
    该列当前**恒为 `NULL`**：评分器尚未实现，而 Backend 刻意不"顺手"填一个
    没人负责的结论。

    人工复核（§6.3）**不产生**它 —— 复核不推进任务、不重算综合等级，
    也不排除 ``REJECTED`` 重新裁决。
    """

    PASS = "PASS"  # 仅 LOW 或无风险 → 可通过
    RECTIFY = "RECTIFY"  # 无 HIGH 但有 MEDIUM → 需整改后签署
    REJECT = "REJECT"  # 存在任一 HIGH → 建议拒绝 / 重大整改


class RiskReviewStatus(StrEnum):
    """法务对**单条风险**的人工复核状态（§6.3）。

    ⚠️ 它不是任务状态，也不产生任何综合结论。四条已冻结的边界：

    * **不推进任务**：``review_task`` 的 ``current_stage`` / ``status`` /
      ``finished_at`` / ``risk_level_final`` / ``conclusion`` / ``summary``
      **都不因人工复核而改变**。``REVIEWED`` 的含义始终是"AI 风险的持久化已完成"，
      不会变成"人工复核完成"（人工复核根本不在流程里 —— 它在 ``persist_result``
      之后，是 Backend 上的一次普通状态更新）
    * **不重算综合等级**：§11.2 的评分归 Agent，Backend 当前既不计算它，
      也**不因为 ``REJECTED`` 而重算**任何综合等级
    * **不从报告概览中扣除**：报告的风险概览按**当前** ``risk_item.risk_level``
      统计，``REJECTED`` 的风险**照常计入** —— 那个区域表达的是
      **AI 审查发现了什么**，不是"人工裁决后剩下什么"
    * **不留历史**：``MODIFIED`` 就地覆盖 ``risk_item.risk_level``，
      AI 原始等级**不可恢复**；没有 ``original_risk_level``，也没有复核历史表
    """

    PENDING = "PENDING"  # AI 产出，未复核
    CONFIRMED = "CONFIRMED"  # 法务确认
    REJECTED = "REJECTED"  # 法务判定误报（⚠️ **不从报告概览中扣除**）
    MODIFIED = "MODIFIED"  # 法务调整了等级（⚠️ 就地覆盖 risk_level，AI 原值不留痕）


class RiskSource(StrEnum):
    """风险来源（§7.2 risk_item.source）。规则与 LLM 同时命中时为 ``RULE_AND_LLM``。"""

    RULE = "RULE"
    LLM = "LLM"
    RULE_AND_LLM = "RULE+LLM"  # 注意：字面值与架构文档 §7.2 保持一致


# =========================================================================== #
# 四、条款与合同分类（架构文档 §7.2）
# =========================================================================== #


class ContractType(StrEnum):
    """合同类型，决定用哪一套规则集（§7.2）。"""

    PURCHASE = "PURCHASE"  # 采购
    SALES = "SALES"  # 销售
    SERVICE = "SERVICE"  # 服务
    LABOR = "LABOR"  # 劳动
    OTHER = "OTHER"


class ClauseType(StrEnum):
    """条款类型（§7.2）。规则可限定只作用于特定条款类型。"""

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


class ExtractMethod(StrEnum):
    """提取方式（§7.2 contract_metadata.extract_method / clause.extract_method）。

    区分来源的意义：LLM 抽取的字段置信度低于正则，前端可据此提示"建议人工核对"。
    """

    RULE = "RULE"  # 正则 / 规则
    REGEX = "REGEX"  # 正则（元数据字段使用此值）
    LLM = "LLM"  # 大模型兜底
    MANUAL = "MANUAL"  # 人工录入 / 修正


class LocatorType(StrEnum):
    """定位方式（§10.4）。

    前端据此外显式选择定位文案，**禁止**用 ``page_number is null`` 做隐式判断：
      * PAGE       → 显示「第 N 页 · 第 M 段」
      * PARAGRAPH  → 只显示「第 M 段」，不得出现任何"第 N 页"文案
    """

    PAGE = "PAGE"  # PDF / 扫描件 / 图片：有真实页码
    PARAGRAPH = "PARAGRAPH"  # DOCX：page_number 恒为 NULL


class BlockType(StrEnum):
    """文档块类型（§7.2 document_block.block_type）。"""

    TITLE = "TITLE"
    PARAGRAPH = "PARAGRAPH"
    TABLE_ROW = "TABLE_ROW"
    HEADER = "HEADER"  # 页眉，归一化阶段会被剔除
    FOOTER = "FOOTER"  # 页脚，归一化阶段会被剔除


# =========================================================================== #
# 五、P3 随 ORM 建模恢复的枚举
#
# 这些枚举在 P2-b 因"无调用方"被收缩；P3 建表后其对应列已落地，故恢复。
# 恢复的是类型定义，**不含**任何业务逻辑。
# =========================================================================== #


class RuleType(StrEnum):
    """规则求值器类型（§7.2 review_rule.rule_type、§11.1）。

    注意求值器本身（RuleEngine / evaluators）属于 P9，本阶段只登记类型。
    """

    KEYWORD = "KEYWORD"  # 关键词命中
    REGEX = "REGEX"  # 正则匹配
    EXISTS = "EXISTS"  # 该条款必须存在
    MISSING = "MISSING"  # 必备条款缺失
    THRESHOLD = "THRESHOLD"  # 数值比较


class UserRole(StrEnum):
    """用户角色（§2.4.2）。

    ⚠️ ``sys_user`` 表属于 P4，本阶段**未创建**；此处仅恢复类型定义，
    供 ``risk_item.reviewer_id`` 等"人工操作者"字段的语义参照使用。
    """

    LEGAL = "legal"  # 法务审查人
    BIZ = "biz"  # 业务经办人
    ADMIN = "admin"  # 系统管理员


class SuggestionType(StrEnum):
    """修改建议类型（§7.2 risk_suggestion.suggestion_type）。"""

    REPLACE = "REPLACE"
    ADD = "ADD"
    DELETE = "DELETE"


class ContractSource(StrEnum):
    """合同来源（§7.2 contract.source）。"""

    UPLOAD = "UPLOAD"  # 本地上传
    APPROVAL_SYSTEM = "APPROVAL_SYSTEM"  # 从审批系统拉取


class AnchorMethod(StrEnum):
    """quote → 坐标的反查方式（§10.5 四级作用域，§7.2 risk_item.anchor_method）。

    该字段既是前端展示依据（``anchor_score < 0.8`` 或 ``CLAUSE_FALLBACK`` 时
    显示「⚠ 定位待核对」），也是 prompt 迭代的量化指标。
    """

    CLAUSE_SCOPED = "CLAUSE_SCOPED"  # L1：条款范围内命中（首选）
    BLOCK_SCOPED = "BLOCK_SCOPED"  # L2：条款覆盖的 block 区间内命中
    DOC_GLOBAL = "DOC_GLOBAL"  # L3：全文唯一命中
    CLAUSE_FALLBACK = "CLAUSE_FALLBACK"  # L4：定位失败，降级为条款级 + 待核对


class LLMScene(StrEnum):
    """LLM 调用场景（§7.2 ai_call_log.scene、§9.1）。

    ⚠️ LLM Provider 与调用链路属于 P10，本阶段只登记类型。
    """

    CLAUSE_REVIEW = "CLAUSE_REVIEW"  # 条款合规审查（核心）
    METADATA_EXTRACT = "METADATA_EXTRACT"  # 元数据抽取兜底
    SUGGESTION = "SUGGESTION"  # 修改建议生成
    SUMMARY = "SUMMARY"  # 审查摘要生成
