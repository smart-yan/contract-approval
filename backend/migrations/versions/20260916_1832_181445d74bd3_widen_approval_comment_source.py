"""widen approval comment source

把 ``approval_comment.source`` 从 ``VARCHAR(16)`` 加宽到 ``VARCHAR(32)``。

它解决什么
--------
§7.2 规定审批单评论的 ``source`` 取值是 ``MANUAL`` 或 **``CONTRACT_REVIEW_SYSTEM``**，
而 P3 建表时给的是 ``VARCHAR(16)`` —— **那个值有 22 个字符，根本存不进去**
（实测 MySQL ``1406 Data too long for column 'source'``）。

也就是说：**回写链路一旦真正写评论，必然踩到这个列宽**。P15-3a 的报告把这条
列为待裁决的阻塞项，架构裁决的结论是加宽到 32（22 个字符 + 余量，
将来的 source 取值不必再为几个字符做一次迁移）。

::

    ALTER TABLE approval_comment MODIFY source VARCHAR(32) NOT NULL COMMENT '...'

⚠️ **只改这一列**：不动 ``comment_type`` / ``external_id`` / ``instance_id`` /
``idempotency_key`` 与那条复合唯一约束，也不动其它任何表。加宽 VARCHAR 在 MySQL 上
是**原地**变更，不改行数、不动索引、不重建表。

⚠️ **downgrade 会变窄，而变窄可能失败**：MySQL 在严格模式下拒绝把已经有数据
"截断"回去（``1406``）。这是**如实的行为**，不是本迁移的缺陷 —— 一旦库里真的存过
``CONTRACT_REVIEW_SYSTEM``，就没有一种"安全的"降级方式能既回到 16 又不丢数据
（退回 16 就等于要求先把那些评论删掉或改写）。因此 downgrade 只保证"**没有超长数据时**
可以干净往返"，并把这个前提写在函数注释里。

Revision ID: 181445d74bd3
Revises: f84b252400df
Create Date: 2026-09-16 18:32:41.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "181445d74bd3"
down_revision: str | None = "f84b252400df"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "approval_comment"
_COLUMN = "source"

#: 加宽后的长度。22 个字符的 ``CONTRACT_REVIEW_SYSTEM`` 只是下限，
#: 取 32 是为了留出余量（与裁决一致）。
_WIDE = 32

#: 原始长度（downgrade 回到这里）。
_NARROW = 16

_COMMENT = "来源：MANUAL / CONTRACT_REVIEW_SYSTEM（§7.2）。回写产生的评论后者"


def upgrade() -> None:
    # ⚠️ 必须显式写 existing_type / existing_nullable：MySQL 的 MODIFY 是整列重定义，
    # 漏了 nullable 会把 NOT NULL 悄悄丢掉。
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(length=_NARROW),
        type_=sa.String(length=_WIDE),
        existing_nullable=False,
        comment=_COMMENT,
    )


def downgrade() -> None:
    # ⚠️ 变窄只在"库里没有超过 16 字符的 source"时才成功（见模块 docstring）。
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(length=_WIDE),
        type_=sa.String(length=_NARROW),
        existing_nullable=False,
        comment=_COMMENT,
    )


__all__ = ["downgrade", "revision", "upgrade"]
