"""Mock 审批系统的本地实现（架构文档 §14.1、§14.2；P15-3b）。

它扮演"外部审批系统"，数据落在本项目自己的两张表里：

* ``approval_instance`` —— 审批单（由 ``scripts/seed_mock_approval.py`` 预置，
  以及将来的"待办拉取"同步进来）
* ``approval_comment``  —— 评论（**本模块是它唯一的业务写入方**）

⚠️ 它是**外部系统**，不是本系统的模块：它只认"审批单 id + 幂等键 + 正文"，
不知道 ``review_task`` / ``writeback_record`` 的存在。反过来，Writeback Service
也**不许**直接碰这两张表 —— 那条边界就是 :mod:`app.integrations.approval`。

幂等怎么实现的（**双层**）
-----------------------
1. **先查后写**：按 ``(instance_id, idempotency_key)`` 查一次，查到就原样返回
2. **数据库唯一约束兜底**：``uq_approval_comment_instance_id_idempotency_key``
   （P15-3a 加的）。并发时两个请求都可能"查不到"，然后都去 INSERT ——
   输的一方撞上唯一约束，捕获后**重新查一次**并把既有那条返回

第 2 层不是多余的：第 1 层的"查"与"写"之间天然有窗口，而 §14.2 要求的是
"重复键返回既有评论不新建"这个**结果**，不是"尽量别重复"。

⚠️ ``external_id`` 是 **Mock 系统自己**给评论起的标识（``MOCK-CMT-<自增 id>``），
与幂等键**无关** —— P15-3a 的裁决明确否掉了"external_id = sha256(key)"那种
"用派生 ID 冒充幂等"的做法。它只是回给调用方存进 ``external_comment_id`` 的标识。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorCode, NotFoundError
from app.core.logging import get_logger
from app.db.models.approval import ApprovalComment, ApprovalInstance
from app.db.session import session_scope
from app.integrations.approval import ApprovalClient, ApprovalCommentRef

logger = get_logger(__name__)

#: 回写产生的评论在 Mock 侧的三列取值（§7.2 给出的字面值）。
#: ⚠️ ``source`` 有 22 个字符 —— P15-3a 之前这一列是 ``varchar(16)``，**写不进去**；
#: 加宽到 32 的那次迁移（181445d74bd3）就是为了这个值。
_COMMENT_TYPE_SYSTEM = "SYSTEM_REVIEW"
_SOURCE_CONTRACT_REVIEW_SYSTEM = "CONTRACT_REVIEW_SYSTEM"

#: 评论人。系统写的评论没有"人"，用一个固定标识而不是伪造一个用户名。
#: 与 §7.2 的 ``author``（评论人姓名，纯字符串、不关联 ``sys_user``）一致。
_AUTHOR = "合同审查系统"

#: ``external_id`` 前缀。形如 ``MOCK-CMT-42``。
_EXTERNAL_ID_PREFIX = "MOCK-CMT-"


class MockApprovalClient(ApprovalClient):
    """把 Mock 审批域当成"外部系统"来调用的客户端。"""

    async def find_comment(self, instance_id: int, idempotency_key: str) -> ApprovalCommentRef | None:
        async with session_scope() as session:
            comment = await _load_comment(session, instance_id, idempotency_key)
            if comment is None:
                return None
            return _to_ref(comment)

    async def post_comment(
        self, instance_id: int, content_md: str, idempotency_key: str
    ) -> ApprovalCommentRef:
        try:
            async with session_scope() as session:
                return await self._post_within(session, instance_id, content_md, idempotency_key)
        except IntegrityError:
            # 并发：两个请求都"查不到"，都走到 INSERT，输的一方撞唯一约束。
            # 事务已回滚 ⇒ **开新事务**重查（旧快照看不见对方刚提交的那一行）。
            async with session_scope() as session:
                existing = await _load_comment(session, instance_id, idempotency_key)
                if existing is None:
                    raise  # 撞的不是幂等键（例如实例被删）⇒ 原样抛，不假装成幂等复用
                logger.info(
                    "并发写评论撞上幂等键，返回既有那条 | instance_id=%s comment_id=%s",
                    instance_id,
                    existing.id,
                )
                return _to_ref(existing)

    async def _post_within(
        self, session: AsyncSession, instance_id: int, content_md: str, idempotency_key: str
    ) -> ApprovalCommentRef:
        # ---- 第 1 层：先查 ----
        existing = await _load_comment(session, instance_id, idempotency_key)
        if existing is not None:
            logger.info(
                "该幂等键已写过，返回既有评论不新建 | instance_id=%s comment_id=%s",
                instance_id,
                existing.id,
            )
            return _to_ref(existing)

        # ---- 审批单必须存在（Mock 侧的"外部系统校验"）----
        # ⚠️ 不同 FK 报错来发现：那会以 IntegrityError 冒出来，被上面的并发分支
        # 当成"幂等键撞了"处理，最后变成一个 500。
        found = await session.scalar(select(ApprovalInstance.id).where(ApprovalInstance.id == instance_id))
        if found is None:
            raise NotFoundError(
                f"审批单 {instance_id} 不存在，无法写入评论",
                code=ErrorCode.NOT_FOUND,
                details={"approval_instance_id": instance_id},
            )

        comment = ApprovalComment(
            instance_id=instance_id,
            author=_AUTHOR,
            content=content_md,
            comment_type=_COMMENT_TYPE_SYSTEM,
            source=_SOURCE_CONTRACT_REVIEW_SYSTEM,
            idempotency_key=idempotency_key,
        )
        session.add(comment)
        await session.flush()

        # ``external_id`` 在拿到自增主键之后才能定 —— 它就是"外部系统自己的评论号"
        comment.external_id = f"{_EXTERNAL_ID_PREFIX}{comment.id}"
        await session.flush()

        logger.info(
            "评论已写入 Mock 审批单 | instance_id=%s comment_id=%s external_id=%s",
            instance_id,
            comment.id,
            comment.external_id,
        )
        return _to_ref(comment)


async def _load_comment(
    session: AsyncSession, instance_id: int, idempotency_key: str
) -> ApprovalComment | None:
    stmt = select(ApprovalComment).where(
        ApprovalComment.instance_id == instance_id,
        ApprovalComment.idempotency_key == idempotency_key,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _to_ref(comment: ApprovalComment) -> ApprovalCommentRef:
    return ApprovalCommentRef(
        # ``external_id`` 理论上可能为空（人工评论），但能走到这里的都是本模块刚写的
        # 或按幂等键查回来的外部评论 —— 有幂等键就一定有 external_id。
        external_id=comment.external_id or "",
        idempotency_key=comment.idempotency_key or "",
    )


__all__ = ["MockApprovalClient"]
