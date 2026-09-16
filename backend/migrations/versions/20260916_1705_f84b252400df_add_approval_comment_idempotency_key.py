"""add approval comment idempotency key

P15-3a：给 ``approval_comment`` 补一列 ``idempotency_key`` 与一条复合唯一约束。

它解决什么
--------
§14.2 要求评论写回"带 ``Idempotency-Key``；**重复键返回既有评论不新建**"，
§12 第 5 步还要求重试前"先查审批系统是否已有该幂等键的评论"（防"写成功了但响应
丢了"）。P3 建表时**没有任何列能承载这个键** —— 那条要求一直无处落地。

架构裁决（P15-3a）明确否掉了两种"绕过去"的做法：用 ``external_id = sha256(key)``
冒充幂等、或用 ``(instance_id, content)`` 判重（把幂等键等同于评论内容）。
因此本迁移**新增列**，而不是在既有列上做文章。

::

    ALTER TABLE approval_comment ADD COLUMN idempotency_key VARCHAR(64) NULL
    ALTER TABLE approval_comment ADD CONSTRAINT uq_approval_comment_instance_id_idempotency_key
        UNIQUE (instance_id, idempotency_key)

⚠️ **为什么列可空**：人工评论（``source=MANUAL``，§7.2 的既有能力）没有
"外部请求身份"这件事，硬要它编一个键就是伪造。MySQL 的唯一索引
**不把多个 NULL 视为冲突**，因此可空列在本迁移建立的约束下仍然允许任意多条
人工评论 —— 这个语义是刻意依赖的（`tests/integration/test_mock_approval_seed.py`
有一条用例专门钉它）。

⚠️ **为什么是复合唯一而不是列级 unique**：幂等键是"外部请求的身份"，
两个审批单各自收到同一个键并不冲突。列级 unique 会把它错当成全局标识。

⚠️ **范围**：只动 ``approval_comment`` 一张表、只加一列一约束。
不改 ``external_id``（P3 裁决 O5：全局唯一还是审批单内唯一**没有裁决**，
本步不替它决定）、不改 ``approval_instance``、不改 ``contract``、
不改 ``writeback_record``、不建 ``writeback_log``。

Revision ID: f84b252400df
Revises: b0c4aef3060b
Create Date: 2026-09-16 17:05:55.945206

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f84b252400df"
down_revision: str | None = "b0c4aef3060b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 约束名按 ``app/db/base.py`` 的 naming_convention（``uq_%(table_name)s_%(column_0_N_name)s``）
#: 推导，**不使用数据库自动生成的随机名**。抽成常量让 upgrade / downgrade 共用一份定义。
_CONSTRAINT_NAME = "uq_approval_comment_instance_id_idempotency_key"

_TABLE = "approval_comment"
_COLUMN = "idempotency_key"
_FK_COLUMN = "instance_id"

#: ``approval_comment.instance_id`` 那条外键**自己**的索引名。
#:
#: ⚠️ MySQL 的实测行为（P11-2 的迁移里记过同一现象，这里又踩到一次）：
#: 建复合唯一 ``(instance_id, idempotency_key)`` 之后，MySQL 发现它**能充当**
#: 外键索引，于是**悄悄删掉**了外键原本自带的单列索引，让新索引成为外键唯一的依靠。
#: 后果：``downgrade`` 里直接 ``DROP INDEX`` 会被拒（MySQL **1553**：
#: ``needed in a foreign key constraint``）—— 必须先给外键补回它自己的索引。
#:
#: 名字与 MySQL 自动创建时用的那个一致（``fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s``）。
_FK_INDEX_NAME = "fk_approval_comment_instance_id_approval_instance"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(length=64),
            nullable=True,
            comment="回写请求的幂等键（外部系统的 Idempotency-Key）。"
            "⚠️ 可空：人工评论没有外部请求身份。UNIQUE(instance_id, idempotency_key)",
        ),
    )
    op.create_unique_constraint(_CONSTRAINT_NAME, _TABLE, [_FK_COLUMN, _COLUMN])

    # 我们刚建的索引已经能充当外键的索引，外键自带的单列索引此刻是**重复**的。
    #
    # 为什么必须显式删：MySQL 只肯自动接管**它自己创建**的那份 —— 全新库上它会
    # 自己消失，但"回滚后再升级"时那份是 ``downgrade`` 用 ``CREATE INDEX`` 显式
    # 建的，MySQL 就不动它。不删的话，同一条路径会因为"库是怎么走到这一步的"
    # 而留下不同的结构。这里用 inspector 判断存在性，让 ``upgrade`` 与库的历史
    # 路径无关（与 P11-2 的迁移同一手法）。
    inspector = sa.inspect(op.get_bind())
    if _FK_INDEX_NAME in {index["name"] for index in inspector.get_indexes(_TABLE)}:
        op.drop_index(op.f(_FK_INDEX_NAME), table_name=_TABLE)


def downgrade() -> None:
    # ⚠️ **先**把外键的索引还回去 —— 否则下一步的 DROP 会被 MySQL 1553 拒绝
    # （而且会等到删列的语句才失败，把库留在半吊子状态）。
    op.create_index(op.f(_FK_INDEX_NAME), _TABLE, [_FK_COLUMN], unique=False)

    # 再反序：先删约束再删列（列还被约束引用着时不能直接删）。
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE, type_="unique")
    op.drop_column(_TABLE, _COLUMN)


__all__ = ["downgrade", "revision", "upgrade"]
