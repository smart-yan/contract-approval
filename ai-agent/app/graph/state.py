"""ContractReviewState —— Agent 工作流在节点之间流转的状态。

设计原则（架构裁决）
------------------
State **不是数据库的副本**，只放"跨节点流转真正需要"的数据。因此这里刻意没有：

* SQLAlchemy ORM 对象 / 完整的 Contract / ReviewTask 对象
* Backend 随时可以重新查询的事实（``task_status``、``task_stage``、合同详情……）
* ``clauses`` / ``metadata`` / ``keywords`` / ``risks`` / ``suggestions`` / ``report``
  —— 这些是后续阶段才增加的字段

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
failure    失败信息，同样供 Conditional Edge 读取
"""

from __future__ import annotations

from typing import TypedDict

from app.schemas.document import ParseResult


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
