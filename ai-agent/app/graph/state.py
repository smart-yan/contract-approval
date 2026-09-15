"""ContractReviewState —— Agent 工作流在节点之间流转的状态。

设计原则（架构裁决）
------------------
State **不是数据库的副本**，只放"跨节点流转真正需要"的数据。因此这里刻意没有：

* SQLAlchemy ORM 对象 / 完整的 Contract / ReviewTask 对象
* Backend 随时可以重新查询的事实（``task_status``、``task_stage``、合同详情……）
* ``suggestions`` / ``report`` —— 尚未实现的阶段才需要的字段，到那时再按需追加

（``clauses`` / ``metadata`` / ``keywords`` 由 P7 加入，``rule_evaluations`` /
``rule_risks`` 由 P8-2 加入 —— 每个字段都对应一个**已经存在的**节点产出，
而不是为将来预留。）

外部依赖（httpx client 等）同样**不在** State 里，它们走
:class:`app.graph.context.ReviewContext` 注入。

⚠️ 一个必须记住的 LangGraph 行为
------------------------------
**凡是要写进 State 的键，都必须在这里声明。**
LangGraph 按 State Schema 建立 channel，节点返回**未声明的键会被静默丢弃**，
不会报错 —— 这是最容易写出"看起来跑了但其实没生效"的地方。

字段分组
-------
input      调用方交给 Agent 的
upload     ``upload_file`` 从 Backend 拿回来的（只保留后续节点真正要用的）
validation ``validate_file`` 的结论，供 Conditional Edge 读取
parse      ``parse_document`` 的产出
understanding P7 三个能力各自的产出
rules      ``rule_snapshot`` 是**输入**，``rule_evaluations`` / ``rule_risks`` 是
           ``rule_review`` 的产出
llm        ``llm_findings`` 是 ``llm_review`` 的产出；``llm_error_*`` 是它的
           **降级**信号（与 ``error_code`` 不是一回事，见字段注释）
risks      ``risks`` 是 ``merge_risks`` 的产出（P9-9）—— 两条来源合并后的
           **最终统一风险列表**，与 ``rule_risks`` / ``llm_findings`` 语义不同
failure    失败信息，同样供 Conditional Edge 读取
"""

from __future__ import annotations

from typing import TypedDict

from app.llm.finding_resolution import ResolvedFinding
from app.risk.schemas import AgentRiskItem
from app.rules.schemas import RuleEvaluationResult, RuleRisk, RuleSetSnapshot
from app.schemas.document import ParseResult
from app.schemas.understanding import Clause, KeywordHit, MetadataItem


class ContractReviewState(TypedDict, total=False):
    """最小合同审查状态。

    ``total=False``：每个节点只写自己负责的那几个键，而不是返回一份完整 State。
    """

    # ------------------------------- input ------------------------------ #
    file_path: str  # 待上传文件在本机的路径
    filename: str  # 原始文件名（Backend 作为展示用 metadata 存下）
    content_type: str | None
    contract_no: str
    title: str
    contract_type: str

    # ------------------------------ upload ------------------------------ #
    contract_id: int
    file_id: int
    review_task_id: int
    sha256: str
    file_type: str
    file_size: int
    reused: bool  # 文件层幂等：Backend 复用了已有 ContractFile
    task_reused: bool  # 任务层幂等：Backend 复用了已有 ReviewTask

    # ---------------------------- validation ---------------------------- #
    file_valid: bool
    validation_errors: list[str]

    # ------------------------------- parse ------------------------------ #
    parse_result: ParseResult | None

    # --------------------------- understanding -------------------------- #
    clauses: list[Clause]  # identify_clauses：条款切分结果
    metadata: list[MetadataItem]  # extract_metadata：从文档抽出的元数据项
    keywords: list[KeywordHit]  # extract_keywords：主题词命中（不判断风险）

    # ------------------------------- rules ------------------------------ #
    #: 本次审查使用的规则集快照 —— **输入**，由编排层放入（P8-2 下一步才是"从 Backend 取"）。
    #: ⚠️ 它与"没有规则的合同类型"不是一回事：``None`` 表示规则快照根本没进 Workflow，
    #: ``rule_review`` 会把它当**输入缺失**处理（写 ``error_code``）；
    #: 而 ``RuleSetSnapshot(rule_set_version=None, rules=[])`` 是**正常结论**（空结论 + 无 error）
    rule_snapshot: RuleSetSnapshot | None
    #: rule_review：**每条规则一条**结论，顺序与 ``rule_snapshot.rules`` 一致。
    #: 三态（MATCHED / NOT_MATCHED / EVALUATION_FAILED）原样保留 ——
    #: "算不出来"因此始终可观察，不会被折叠进"没命中"
    rule_evaluations: list[RuleEvaluationResult]
    #: rule_review：命中产生的风险，按 ``rule_evaluations`` 顺序展平。
    #: 元素就是 ``RuleRisk`` 本身（**不重新包装**），
    #: 因此 ``paragraph_index`` / ``quote`` 等定位信息不会被搬运时丢掉
    rule_risks: list[RuleRisk]

    # -------------------------------- llm ------------------------------- #
    #: llm_review：**已定位**的模型发现（P9-7 起）。
    #: 元素是 :class:`~app.llm.finding_resolution.ResolvedFinding` ——
    #: 模型的说法（``.finding``）**加上** Agent 核出来的位置
    #: （``paragraph_index`` / ``original_text`` / ``quote`` / ``anchor_method``）。
    #:
    #: ⚠️ 字段名保留 ``llm_findings``（本轮不改名），但它**不是风险项**：
    #: 没有 ``source``、没有与规则结果的合并、没有评分。
    #: 与规则侧同理：模型跑失败时这里**保持缺失**，不写空列表
    #: （空列表会被读成"模型看了，没发现问题"）
    llm_findings: list[ResolvedFinding]

    #: LLM 审查的**降级**信号（P9-6a）。刻意**不**写进 ``error_code``：
    #: §9.1 第 4 道防线规定"本批降级为**仅规则引擎结果**并在任务上标记 warning，
    #: 绝不让整个任务失败" —— LLM 失败时规则结果仍然完整可用，
    #: 若借用 ``error_code``，API 会把一次"规则部分照常可用"的审查报成 rejected。
    #: 于是它单独占一条通道：**整次审查的失败**（``error_code``）与
    #: **一次降级**（``llm_error_code``）是两件事，各有各的分流。
    llm_error_code: str | None
    llm_error_message: str | None

    # ------------------------------- risks ------------------------------ #
    #: merge_risks：**最终的统一风险列表**（P9-9 接入）。
    #: 规则与模型两条来源经 ``unify`` 映射、再由 ``merge`` 收敛后的结果，
    #: 元素是 :class:`~app.risk.schemas.AgentRiskItem`。
    #:
    #: 它与上面两个键**语义不同**，三者互不替代（各有各的消费者）：
    #:
    #: ==================  ==============================================
    #: ``rule_risks``      只有规则来源，未合并（``RuleRisk``）
    #: ``llm_findings``    只有模型来源，**已定位但还不是风险项**（``ResolvedFinding``）
    #: ``risks``           **两者合并之后**的统一风险项 —— 评分 / 落库 / 报告只认它
    #: ==================  ==============================================
    #:
    #: ``rule_risks`` / ``llm_findings`` **不因合并而被覆盖或改写**：它们既是回溯的
    #: 依据（"这条风险为什么成立"要能指回规则与模型各自的原始说法），
    #: 也是"合并到底并掉了什么"的唯一证据。
    #:
    #: ⚠️ ``risks`` **有值**（含空列表）表示**合并跑过**，但**不表示这次审查成功** ——
    #: 规则审查失败时图并不会停（``error_code`` 由 ``rule_review`` 写下，
    #: 后面照常走到这里），只是规则侧没有输入。判"这次审查可用吗"仍然只看
    #: ``error_code`` / ``has_usable_document``，不看这个键。
    risks: list[AgentRiskItem]

    # ------------------------------ failure ----------------------------- #
    error_code: str | None
    error_message: str | None


def has_usable_document(state: ContractReviewState) -> bool:
    """State 里是否拿到了**可用的文档**。

    这是"解析这一步算不算成功"的**唯一判据**，Graph 的分流与对外响应都读它，
    不允许在别处再写一遍 ``status == "FAILED"``。

    可用的定义
    ---------
    * ``PARSED`` —— 解析出内容了
    * ``EMPTY``  —— 解析成功，但文档本身没内容（**数据问题**，不是我们没读出来）
    * ``parse_result`` 缺失 —— **不可用**（fail-closed：没跑过解析就不能当成拿到了文档）
    * ``FAILED`` —— 不可用（**系统问题**，需要人工或重试介入）
    """
    result = state.get("parse_result")
    return result is not None and result.status != "FAILED"


__all__ = ["ContractReviewState", "has_usable_document"]
