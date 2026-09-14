"""创建数据库（架构文档 §17 的 P2 交付项：MySQL 建库）。

职责边界
--------
本脚本**只负责建库**（``CREATE DATABASE IF NOT EXISTS``）。
P3 会把「执行 Alembic 迁移」与「灌入 seed 数据」接进来，届时成为完整初始化入口。

为什么用同步驱动直连，而不是复用 ``app.db.session`` 的引擎
--------------------------------------------------------
建库时目标库**尚不存在**，而运行时的 DSN 里已经带上了库名
（``.../contract_approval?charset=utf8mb4``），用它连必然失败。
所以这里用 pymysql 直连到服务器（不指定库），这是运维脚本的合理特例。

安全
----
* 密码只在内存中使用，**不打印、不写日志**；
* 任何异常文本都先经 ``scrub_password()`` 清洗再输出；
* 库名做了白名单校验，因为 DDL 无法参数化。

用法::

    cd backend
    uv run python scripts/init_db.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 允许以 `python scripts/init_db.py` 方式直接运行：把 backend/ 加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymysql

from app.core.config import get_settings
from app.db.session import scrub_password

#: 目标字符集与排序规则（架构文档 §0.2 第 3 项：MySQL 8 的 utf8mb4_0900_ai_ci）
CHARSET = "utf8mb4"
COLLATION = "utf8mb4_0900_ai_ci"

#: 库名白名单：DDL 不支持参数化占位符，只能靠校验防注入
_DB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _validate_db_name(name: str) -> str:
    if not _DB_NAME_PATTERN.match(name):
        raise SystemExit(f"[FAIL] 非法的数据库名 {name!r}：只允许字母、数字、下划线，长度 1-64")
    return name


def main() -> int:
    settings = get_settings()
    db_name = _validate_db_name(settings.mysql_db)
    safe_dsn = settings.database_url_safe

    print(f"[INFO] 目标服务器 : {settings.mysql_host}:{settings.mysql_port}")
    print(f"[INFO] 目标数据库 : {db_name}")
    print(f"[INFO] 连接串     : {safe_dsn}")

    try:
        conn = pymysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            charset=CHARSET,
            connect_timeout=10,
            autocommit=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 无法连接 MySQL：{type(exc).__name__}: {scrub_password(str(exc))}")
        print("[HINT] 请确认 MySQL 服务已启动（Windows 服务名通常为 MySQL84）")
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET {CHARSET} COLLATE {COLLATION}"
            )
            cur.execute(
                "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
                (db_name,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 建库失败：{type(exc).__name__}: {scrub_password(str(exc))}")
        return 1
    finally:
        conn.close()

    if not row:
        print(f"[FAIL] 建库后未能在 information_schema 中查到 {db_name}")
        return 1

    charset, collation = row
    print(f"[ OK ] 数据库已就绪：字符集={charset} 排序规则={collation}")

    if charset != CHARSET or collation != COLLATION:
        print(f"[WARN] 期望 {CHARSET}/{COLLATION}，实际 {charset}/{collation}（库已存在且未改动）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
