"""审批系统的**防腐层**接口（架构文档 §14.1、§15；P15-3b）。

它是什么
-------
本系统真正的"甲方"是外部审批系统（钉钉/飞书/泛微/OA）—— 我们接不到，因此：

::

    Writeback Service ──► ApprovalClient（本模块的抽象）
                              ├── MockApprovalClient   当前：写在本地 Mock 域两张表里
                              └── HttpApprovalClient   将来：真对接（§15 明确为"空实现 + 接口文档"）

价值不在"现在能跑"，而在**将来换实现时业务代码零改动**：Writeback Service 只认
这个接口，不知道"评论"到底落在 MySQL 里还是飞书的 HTTP 接口上。

**边界（本步的核心约束）**
------------------------
* Writeback Service **不许**直接 INSERT ``approval_comment``、**不许**散落 Approval ORM 查询
* Mock 实现（``mock_approval.py``）负责当前项目里的模拟
* 不引入第二个 HTTP 服务 / Docker / 额外数据库 —— 当前项目内 Mock Client + MySQL 足够

幂等契约（**接口级要求，不是某个实现的可选优化**）
----------------------------------------------
§14.2 规定评论写回"带 ``Idempotency-Key``；**重复键返回既有评论不新建**"。因此：

============================  ==================================================
``post_comment``              同一个 ``(instance_id, idempotency_key)`` 调两次，
                              **只产生一条评论**，两次都返回**同一个** external_id
``find_comment``              按同一对键查回那条评论；没有则返回 ``None``
============================  ==================================================

这两条合起来才让"**外部已经写成功、但响应丢了**"这个经典场景可恢复（§12 第 5 步）：
重试时先 ``find_comment``，找到了就说明外部其实已经成功 —— **绝不能**因为本地记录
还停在 ``writing`` 就认为外部没写成。⚠️ 本地的 ``UNIQUE(idempotency_key)`` 只能保证
"本地不重复建记录"，它对"外部到底执行了没有"**一无所知**。

调用方拿到的 ``external_id`` 落 ``writeback_record.external_comment_id`` ——
**是 Mock/外部系统自己给的标识**，不参与任何本地幂等计算（P15-3a 明确否掉了
"用 external_id = sha256(key) 来模拟幂等"那条路）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ApprovalCommentRef:
    """外部审批系统里一条评论的**标识**。

    ⚠️ 刻意不带 ``content`` / ``author``：调用方（Writeback Service）需要的是
    "外部那条评论存不存在、它的 id 是什么"，而不是把外部系统的数据搬到自己的
    逻辑里来用。真要展示评论内容，那是 Mock 审批页面（后续步骤）的事。
    """

    external_id: str
    """外部系统的评论 ID —— 落 ``writeback_record.external_comment_id``。"""

    idempotency_key: str
    """写这条评论时用的幂等键（原样带回，便于调用方核对"是不是我要的那条"）。"""


class ApprovalClient(ABC):
    """外部审批系统的客户端接口。

    ⚠️ 两个方法都**不抛**业务异常给调用方判断"成功/失败"以外的东西 ——
    网络/超时之类的故障以异常形式抛出（``ApprovalSystemError``），由 Writeback
    Service 收敛成 ``failed`` + ``error_msg``（§6.2 的 ``writing → failed``）。
    """

    @abstractmethod
    async def find_comment(self, instance_id: int, idempotency_key: str) -> ApprovalCommentRef | None:
        """按幂等键找回已经写过的评论；没有则 ``None``。

        这是**恢复**路径的第一动作（§12 第 5 步）：重试之前先问外部"你那边是不是
        已经有了"，而不是盲目再发一条。
        """

    @abstractmethod
    async def post_comment(
        self, instance_id: int, content_md: str, idempotency_key: str
    ) -> ApprovalCommentRef:
        """把审查意见写进审批单的评论区。

        **必须幂等**：同一个 ``(instance_id, idempotency_key)`` 重复调用只产生一条
        评论，并返回同一个 ``external_id``（见模块 docstring）。
        """


class ApprovalSystemError(RuntimeError):
    """与审批系统交互失败（连不上 / 超时 / 非 2xx）。

    刻意**不是** ``app.core.errors.AppError`` 的子类：它不是"业务判定"，
    而是"这次调用没成"，由 Writeback Service 转成 ``writing → failed``
    （失败原因进 ``writeback_record.error_msg``），而不是直接变成 HTTP 响应码。
    """


def get_approval_client() -> ApprovalClient:
    """当前生效的审批系统客户端 —— **换实现只改这一个函数**。

    与项目既有风格一致（不用 FastAPI 的 ``Depends``：Backend 里没有任何一处用它，
    服务都是直接 import 的）。将来接真实系统时，这里按配置返回
    ``HttpApprovalClient``，Writeback Service 一行都不用动。
    """
    from app.integrations.mock_approval import MockApprovalClient

    return MockApprovalClient()


__all__ = [
    "ApprovalClient",
    "ApprovalCommentRef",
    "ApprovalSystemError",
    "get_approval_client",
]
