"""时间工具（架构文档 §2.1 utils/datetime_utils.py）。

时间语义（全项目统一）
----------------------
* 数据库以 **MySQL DATETIME(3)** 存储，**统一存 UTC**（架构文档 §7.2）。
* 写入时使用 **naive UTC**（``datetime`` 不带 tzinfo）。

为什么用 naive UTC 而不是 aware datetime
----------------------------------------
MySQL 的 DATETIME 列不保存时区信息。若向它写入带 tzinfo 的 aware datetime，
PyMySQL/aiomysql 会用 ``str(obj)`` 序列化，产出形如
``2026-09-14 10:00:00+00:00`` 的字符串 —— 轻则被静默截断、重则直接报错，
而且不同驱动的行为还不一致。因此约定：

* **写库**：一律 ``utcnow()``（naive UTC）；
* **读库**：读到的时间一律按 UTC 解释；
* **进出接口**：在 API 层（P4+）统一转成 ISO8601 带 Z 的字符串，不把 naive 时间直接丢给前端。

这样"数据库里是什么时区"这件事只有一个答案，不需要依赖 MySQL 的
``time_zone`` 会话变量，也不会因为换机器/换时区而出现 8 小时偏移。
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """返回当前 UTC 时间（**naive**，即不带 tzinfo），用于写库。

    >>> isinstance(utcnow(), datetime)
    True
    >>> utcnow().tzinfo is None
    True
    """
    return datetime.now(UTC).replace(tzinfo=None)
