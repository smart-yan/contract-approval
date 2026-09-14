"""Alembic 配置守卫测试。

这些断言看起来像"检查静态文本"，但它们保护的每一条都是**会真实炸掉的坑**：

* ``alembic.ini`` 混入非 ASCII ⇒ 中文 Windows 上 configparser 按 GBK 解码直接抛
  UnicodeDecodeError，Alembic **完全无法启动**（P2-c 实际踩到过）；
* ``alembic.ini`` 里出现 DSN ⇒ 密码多了一份副本，且可能与 .env 不一致而迁移到错误的库；
* 在线迁移必须用 NullPool ⇒ 否则迁移连接会常驻连接池。
"""

from __future__ import annotations

import configparser
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ENV_PY = BACKEND_DIR / "migrations" / "env.py"


def test_alembic_ini_stays_pure_ascii() -> None:
    """回归守卫：configparser 按系统 locale 读 ini，非 ASCII 会让 Alembic 起不来。"""
    raw = ALEMBIC_INI.read_bytes()
    offenders = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert not offenders, (
        f"alembic.ini 含 {len(offenders)} 个非 ASCII 字节（首个位于 offset {offenders[0][0]}）。"
        " 该文件必须保持纯 ASCII，中文说明请写到 migrations/env.py。"
    )


def test_alembic_ini_is_readable_by_configparser() -> None:
    """用与 Alembic 相同的方式解析一次，确保语法本身没问题。"""
    parser = configparser.ConfigParser()
    parser.read(ALEMBIC_INI, encoding="ascii")
    assert "alembic" in parser


def test_alembic_ini_declares_no_dsn() -> None:
    """DSN 唯一来源是 .env；ini 里出现 url 就等于密码有了第二份副本。"""
    text = ALEMBIC_INI.read_text(encoding="ascii")
    parser = configparser.ConfigParser()
    parser.read_string(text)
    assert not parser.has_option("alembic", "sqlalchemy.url")
    # 注释里可以提到驱动名（如 mysql+aiomysql），但不允许出现真正可用的连接串
    assert "://" not in text, "alembic.ini 中不应出现任何数据库连接串"


def test_script_location_points_to_migrations_directory() -> None:
    parser = configparser.ConfigParser()
    parser.read(ALEMBIC_INI, encoding="ascii")
    location = parser.get("alembic", "script_location")
    assert (BACKEND_DIR / location).is_dir(), f"script_location={location} 不存在"


def test_env_py_binds_target_metadata_to_base() -> None:
    """autogenerate 的对比目标必须是 app.db.base.Base.metadata，否则 P3 建表不会被感知。"""
    source = ENV_PY.read_text(encoding="utf-8")
    assert "from app.db.base import Base" in source
    assert "target_metadata = Base.metadata" in source


def test_env_py_uses_nullpool_for_online_migrations() -> None:
    """在线迁移用独立引擎 + NullPool：不在业务连接池里留下常驻连接。"""
    source = ENV_PY.read_text(encoding="utf-8")
    assert "poolclass=pool.NullPool" in source
    assert "create_async_engine(\n        settings.database_url,\n        poolclass=pool.NullPool," in source


def test_env_py_pins_utc_session_time_zone_for_online_migrations() -> None:
    """在线迁移引擎必须与运行期引擎共用 build_connect_args()（含 UTC 会话时区）。"""
    source = ENV_PY.read_text(encoding="utf-8")
    assert "build_connect_args" in source
    assert "connect_args=build_connect_args()" in source


def test_env_py_offline_mode_does_not_fake_a_session_time_zone() -> None:
    """离线模式只渲染 SQL、没有数据库会话，不得对它做任何时区包装。"""
    source = ENV_PY.read_text(encoding="utf-8")
    offline_body = source.split("def run_migrations_offline()")[1].split("def do_run_migrations")[0]
    assert "connect_args" not in offline_body
    assert "time_zone" not in offline_body


def test_env_py_offline_mode_uses_sync_dsn() -> None:
    """离线模式只生成 SQL，用同步 DSN 即可，不必拉起异步驱动。"""
    source = ENV_PY.read_text(encoding="utf-8")
    assert "url=settings.database_url_sync" in source


def test_env_py_does_not_reuse_runtime_engine() -> None:
    """迁移引擎必须与业务引擎解耦：env.py 不得 import app.db.session。"""
    source = ENV_PY.read_text(encoding="utf-8")
    assert "from app.db.session" not in source
