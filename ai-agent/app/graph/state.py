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
llm        ``llm_findings`` 是 ``llm_review`` 的产出
failure    失败信息，同样供 Conditional Edge 读取
"""

from __future__ import annotations

from typing import TypedDict

from app.llm.findings import LLMFinding
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
    #: llm_review：模型报出的发现（P9-5）。
    #: ⚠️ 它们是**模型的原始发现**，不是风险项 —— 还没做定位（P9-3/P9-4 的能力
    #: 尚未接进节点）、没有 ``source`` 也没有最终段落号。
    #: 与规则侧同理：模型跑失败时这里**保持缺失**，不写空列表
    #: （空列表会被读成"模型看了，没发现问题"）
    llm_findings: list[LLMFinding]

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
