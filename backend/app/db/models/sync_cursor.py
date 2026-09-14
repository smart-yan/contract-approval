"""同步游标模型：``sync_cursor``（架构文档 §7.2 末行、§4.1）。

用于「增量拉取审批系统待办」：记录每个数据源上次同步到的位置，
避免每次全量拉取。

设计说明
--------
``source`` **P3 不加 UNIQUE**（架构裁决 E）：§7.2 只给了
``source, cursor_value, last_sync_at`` 三个字段，**没有**说明
"一个 source 是否只对应一行游标"。在语义明确之前不猜，保持文档原定义。

拉取逻辑属于 **P5（合同接入）/ P14（Mock 审批）**，本模块只建表。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseMixin


class SyncCursor(Base, BaseMixin):
    """增量拉取游标（§7.2）。"""

    __tablename__ = "sync_cursor"

    source: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="数据源标识。⚠️ 唯一性未定义，P3 不加 UNIQUE（裁决 E）"
    )
    cursor_value: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="游标值（如上次拉取到的外部 ID）"
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=3), nullable=True, comment="上次同步时刻（UTC）"
    )
