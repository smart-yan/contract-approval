"""add query indexes for workbench

P11-2：为 P11 的查询补 5 个**普通非唯一索引**。

它们纯粹是查询优化，**不改变任何业务语义**：没有新列、没有改类型、
没有动 UNIQUE / FK / 默认值。

::

    clause(task_id)                WHERE task_id = ?          工作台取条款
    contract_metadata(task_id)     WHERE task_id = ?          工作台取元数据
    risk_item(task_id)             WHERE task_id = ?          工作台取风险
    review_task(contract_id, id)   WHERE contract_id = ?      列表取"最新任务"
                                   ORDER BY id DESC
    contract(created_at, id)       ORDER BY created_at DESC,  列表排序
                                   id DESC

为什么需要
--------
P3 建表时只有 §7.2 点名的两个队列索引（``idx_claim`` / ``idx_heartbeat``）
与 ``document_block`` 的两个坐标索引。上表这五处**一个新索引都没有** ——
它们全是 P11 才出现的查询形态，在 P10 之前根本没有读路径。

⚠️ 索引与排序键**一一对应**，这不是巧合：
``review_task(contract_id, id)`` 的第二列就是排序键，``contract(created_at, id)``
同理 —— 这样 MySQL 能直接用索引顺序输出，不必额外 filesort。
把排序列写进索引是这里唯一"设计过"的地方，其余四列都是等值查询的前缀。

⚠️ **已知的模型/数据库漂移（如实记录，本步不处理）**
这五个索引只存在于 migration 里，**没有**写进 ORM 模型的 ``__table_args__``
（本步明确禁止修改 Model）。后果：将来的 ``alembic revision --autogenerate``
会把它们当成"数据库里多出来的东西"，生成 ``drop_index``。
项目里既有的索引（``idx_claim`` / ``ix_document_block_*``）**两侧都有**，所以
本文件是第一次出现这种漂移 —— 下次 autogenerate 前需要先决定：
把它们补回模型，还是在 autogenerate 后手工剔除这几条 drop。

⚠️ **`review_task(contract_id, id)` 与外键的关系（踩过一次，务必保留注释）**
MySQL 在加这个复合索引时会**悄悄接管**原来为外键自动创建的单列索引
（实测确认），因此 ``downgrade`` 里必须先给外键补回一个单列索引才能删它，
否则报 1553 且会在删掉前面几个索引之后才失败。

Revision ID: b0c4aef3060b
Revises: 313b0960b510
Create Date: 2026-09-15 15:06:38.753070

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b0c4aef3060b"
down_revision: str | None = "313b0960b510"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 索引名 → (表名, 列列表)。名字按 ``app/db/base.py`` 的 naming_convention
#: （``ix_%(table_name)s_%(column_0_N_name)s``）推导，**不使用数据库自动生成的随机名**。
#: 单独抽成常量是为了让 ``upgrade`` 与 ``downgrade`` 共用同一份定义 ——
#: 两边各抄一遍迟早会漏掉一个，而 downgrade 漏索引是**静默的**（回滚"成功"了，索引还在）。
_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_clause_task_id", "clause", ["task_id"]),
    ("ix_contract_metadata_task_id", "contract_metadata", ["task_id"]),
    ("ix_risk_item_task_id", "risk_item", ["task_id"]),
    ("ix_review_task_contract_id_id", "review_task", ["contract_id", "id"]),
    ("ix_contract_created_at_id", "contract", ["created_at", "id"]),
)


#: 本次迁移会**接管**的外键索引：(表, 外键自己的索引名, 外键列)。
#:
#: MySQL 的实测行为（在真实库上做过受控实验，结论确定）：
#: 建表时若外键列没有可用索引，MySQL 会自动建一个；而当我们再加一个**能充当该外键
#: 索引**的索引时，MySQL 会**悄悄把自动建的那个删掉**，让新的成为外键唯一的依靠。
#:
#: 后果：`downgrade` 里直接 `DROP` 会被拒（MySQL 1553：needed in a foreign key
#: constraint），而且**会在删掉前面几个索引之后才失败**，把库留在半吊子状态。
#: 因此回滚时必须**先**给这些外键补回它们自己的单列索引，**再**删我们的。
#:
#: 名字按 naming_convention 的 ``fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s``
#: 推导 —— 与 MySQL 自动创建时用的名字一致，也就是这几个外键**本来就有**的索引。
#: 补回去之后，整轮 downgrade 的净效果与 upgrade 之前**完全一致**。
_FK_INDEXES_TAKEN_OVER: tuple[tuple[str, str, list[str]], ...] = (
    ("clause", "fk_clause_task_id_review_task", ["task_id"]),
    ("contract_metadata", "fk_contract_metadata_task_id_review_task", ["task_id"]),
    ("risk_item", "fk_risk_item_task_id_review_task", ["task_id"]),
    ("review_task", "fk_review_task_contract_id_contract", ["contract_id"]),
)


def upgrade() -> None:
    # ``contract(created_at, id)`` 不在接管清单里：contract 表没有任何外键。
    for name, table, columns in _INDEXES:
        op.create_index(op.f(name), table, columns, unique=False)

    # 我们刚建的索引已经能充当这几个外键的索引，外键自带的单列索引此刻是**重复**的。
    #
    # 为什么要显式删：MySQL 只肯自动接管**它自己创建**的那份 —— 全新库上它会自己消失，
    # 但"回滚后再升级"时那份是 ``downgrade`` 用 ``CREATE INDEX`` 显式建的，
    # MySQL 就不会动它。不删的话，同一条路径会因为"库是怎么走到这一步的"而留下
    # 不同的结构（多余索引会让这四张表的写入变慢）。
    # 这里用 inspector 判断存在性，让 ``upgrade`` **与库的历史路径无关**。
    inspector = sa.inspect(op.get_bind())
    for table, fk_index, _ in _FK_INDEXES_TAKEN_OVER:
        if fk_index in {index["name"] for index in inspector.get_indexes(table)}:
            op.drop_index(op.f(fk_index), table_name=table)


def downgrade() -> None:
    # 先把外键的索引还回去 —— 否则下一步的 DROP 会被 MySQL 1553 拒绝
    for table, fk_index, columns in _FK_INDEXES_TAKEN_OVER:
        op.create_index(op.f(fk_index), table, columns, unique=False)

    # 再反序删除本次新增的索引（顺序与建的对称，读起来能一眼对上）
    for name, table, columns in reversed(_INDEXES):
        op.drop_index(op.f(name), table_name=table)


__all__ = ["downgrade", "revision", "upgrade"]
