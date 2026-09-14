"""ORM 模型注册入口。

**导入本包即完成所有模型的注册** —— 这是 ``Base.metadata`` 能看见全部表的唯一途径，
Alembic autogenerate 与 ``create_all`` 都依赖它。

因此：

* ``migrations/env.py`` 会 ``from app.db import models`` 来触发注册；
* 新增模型文件后，**必须**在此处补一行导入，否则该表不会进入迁移。

P3 建立的 15 张表（架构文档 §7.2 中字段定义完整的部分）：

============================  ==========================================
模型                          表名
============================  ==========================================
``Contract``                  ``contract``
``ContractFile``              ``contract_file``
``ReviewTask``                ``review_task``
``DocumentBlock``             ``document_block``
``ContractMetadata``          ``contract_metadata``
``Clause``                    ``clause``
``ReviewRuleSet``             ``review_rule_set``
``ReviewRule``                ``review_rule``
``RiskItem``                  ``risk_item``
``RiskSuggestion``            ``risk_suggestion``
``WritebackRecord``           ``writeback_record``
``AiCallLog``                 ``ai_call_log``
``ApprovalInstance``          ``approval_instance``
``ApprovalComment``           ``approval_comment``
``SyncCursor``                ``sync_cursor``
============================  ==========================================

**刻意未创建**（字段定义不完整，经架构裁决延后）：
``sys_user``(P4)、``task_event``(P6)、``annotation``(P11)、
``standard_clause``(P13)、``report``(P12)、``writeback_log``(P12)。
"""

from __future__ import annotations

from app.db.models.ai_call_log import AiCallLog
from app.db.models.approval import ApprovalComment, ApprovalInstance
from app.db.models.contract import Contract, ContractFile
from app.db.models.document import Clause, ContractMetadata, DocumentBlock
from app.db.models.review_task import ReviewTask
from app.db.models.risk import RiskItem, RiskSuggestion
from app.db.models.rule import ReviewRule, ReviewRuleSet
from app.db.models.sync_cursor import SyncCursor
from app.db.models.writeback import WritebackRecord

__all__ = [
    "AiCallLog",
    "ApprovalComment",
    "ApprovalInstance",
    "Clause",
    "Contract",
    "ContractFile",
    "ContractMetadata",
    "DocumentBlock",
    "ReviewRule",
    "ReviewRuleSet",
    "ReviewTask",
    "RiskItem",
    "RiskSuggestion",
    "SyncCursor",
    "WritebackRecord",
]
