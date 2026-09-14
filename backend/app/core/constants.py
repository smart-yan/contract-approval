"""全项目共享枚举词汇。

架构文档：§2.1 core/constants.py（枚举：任务状态/回写状态/风险等级/条款类型...）。

本模块**只有枚举声明，不含任何业务逻辑**，是 P3 建表、P6 状态机、P7 解析
共同依赖的「词汇表」。每个枚举都标注了架构文档出处，便于回溯与评审。

范围纪律
--------
只登记**当前阶段或近期基础设施已有实际调用方**的枚举。远期模块
（P4 认证、P5 接入、P9 规则引擎、P10 LLM、P12 回写）的枚举在进入对应阶段时
按需追加 —— 避免"声明先行、无人使用"的维护噪声。

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
    """风险等级（§11.2 综合等级 = 所有有效风险中的最高等级，高风险一票定级）。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ReviewConclusion(StrEnum):
    """审查结论（§11.2）。"""

    PASS = "PASS"  # 仅 LOW 或无风险 → 可通过
    RECTIFY = "RECTIFY"  # 无 HIGH 但有 MEDIUM → 需整改后签署
    REJECT = "REJECT"  # 存在任一有效 HIGH → 建议拒绝 / 重大整改


class RiskReviewStatus(StrEnum):
    """法务人工复核状态（§6.3）。综合等级基于「有效风险」重算，REJECTED 不计入。"""

    PENDING = "PENDING"  # AI 产出，未复核
    CONFIRMED = "CONFIRMED"  # 法务确认
    REJECTED = "REJECTED"  # 法务判定误报，不计入综合结论
    MODIFIED = "MODIFIED"  # 法务调整了等级或建议，以人工等级为准


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
