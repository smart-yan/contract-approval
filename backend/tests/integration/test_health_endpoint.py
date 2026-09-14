"""lifespan 与 /health 的集成测试（**需要真实 MySQL**）。

MySQL 不可用时整个模块自动跳过。

这里验证的是**单元测试无法覆盖的部分**：
真正跑一遍 lifespan（启动 → 服务 → 关闭），确认真实数据库下的健康检查，
以及关闭时连接池与执行器确实被释放。
"""

from __future__ import annotations

import pymysql
import pytest
from fastapi.testclient import TestClient

import app.db.session as session_module
from app.core import executors
from app.core.config import get_settings
from app.main import REQUEST_ID_HEADER, app


def _db_available() -> tuple[bool, str]:
    settings = get_settings()
    if not settings.mysql_password.get_secret_value():
        return False, ".env 中未配置 MYSQL_PASSWORD"
    try:
        conn = pymysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            database=settings.mysql_db,
            charset="utf8mb4",
            connect_timeout=3,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}（MySQL 服务未启动或库不存在？）"
    else:
        conn.close()
        return True, ""


_AVAILABLE, _REASON = _db_available()

pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=f"MySQL 不可用：{_REASON}")


# --------------------------------------------------------------------------- #
# lifespan：启动
# --------------------------------------------------------------------------- #
def test_lifespan_starts_engine_and_accepts_requests() -> None:
    with TestClient(app) as client:
        assert session_module._engine is not None, "启动后引擎应已创建"
        assert executors._thread_pool is not None, "启动后受控线程池应已就绪"

        response = client.get("/health")
        assert response.status_code == 200


def test_lifespan_sets_started_at_for_uptime() -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["uptime_seconds"] is not None
        assert body["uptime_seconds"] >= 0


# --------------------------------------------------------------------------- #
# /health：真实数据库下的行为
# --------------------------------------------------------------------------- #
def test_health_reports_real_database_info() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200

        body = response.json()
        assert body["status"] == "ok"

        db = body["checks"]["database"]
        assert db["ok"] is True
        assert db["database"] == get_settings().mysql_db
        assert db["server_version"].startswith("8.")
        assert db["charset"] == "utf8mb4"


def test_health_returns_request_id_header() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.headers.get(REQUEST_ID_HEADER)


def test_health_does_not_create_ocr_executor() -> None:
    """懒加载守卫：黄金链路只跑 DOCX，健康检查不得把 OCR 进程池拉起来。"""
    with TestClient(app) as client:
        for _ in range(3):
            client.get("/health")

        assert executors._ocr_executor is None
        assert client.get("/health").json()["checks"]["executors"]["ocr_executor_created"] is False


def test_health_does_not_hold_a_database_connection() -> None:
    """健康检查必须是短生命周期：调用结束后连接池里不能有借出未还的连接。"""
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/health").status_code == 200

        checked_out = session_module.get_engine().pool.checkedout()
        assert checked_out == 0, f"健康检查后仍有 {checked_out} 条连接未归还"


def test_lifespan_uses_the_shared_utc_configured_engine() -> None:
    """lifespan 启动的必须是共享引擎工厂产出的那个引擎。

    这条断言与 P2-c 的 ``test_session_time_zone_is_utc`` 串起来构成完整证据链：
    应用用的引擎 == ``app.db.session.get_engine()`` ⊨ 会话时区为 +00:00。

    刻意**不在这里操作引擎**：它由 TestClient 自己的事件循环创建，
    在别的循环里 await 它会直接报错（跨事件循环使用 AsyncEngine 是非法的）。
    """
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert session_module._engine is not None
        assert session_module._engine is session_module.get_engine()


def test_shared_connect_args_still_pin_utc_session_time_zone() -> None:
    """P2-c 确定的会话时区约定必须继续成立。

    直接用共享的 ``build_connect_args()`` 建一条同步连接来验证 ——
    不依赖应用引擎、也不依赖事件循环，因此不受 TestClient 生命周期影响。
    """
    from app.db.base import build_connect_args

    settings = get_settings()
    args = build_connect_args()
    conn = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password.get_secret_value(),
        database=settings.mysql_db,
        charset="utf8mb4",
        init_command=args["init_command"],
        connect_timeout=args["connect_timeout"],
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT @@session.time_zone, NOW(3), UTC_TIMESTAMP(3)")
            tz, now3, utc3 = cur.fetchone()
    finally:
        conn.close()

    assert tz == "+00:00"
    assert abs((now3 - utc3).total_seconds()) < 1


# --------------------------------------------------------------------------- #
# lifespan：关闭
# --------------------------------------------------------------------------- #
def test_lifespan_disposes_engine_on_shutdown() -> None:
    """关闭时必须调用 dispose_engine()，不留连接池资源。"""
    with TestClient(app) as client:
        client.get("/health")
        assert session_module._engine is not None

    assert session_module._engine is None, "shutdown 未释放数据库引擎"


def test_lifespan_shuts_down_executors_on_shutdown() -> None:
    with TestClient(app) as client:
        client.get("/health")
        assert executors._thread_pool is not None

    assert executors._thread_pool is None, "shutdown 未关闭线程池"
    assert executors._ocr_executor is None


def test_app_can_be_restarted_after_shutdown() -> None:
    """关闭要可重入：反复启动/关闭不应报错，也不能复用已关闭的资源。"""
    with TestClient(app) as client:
        first_engine = session_module._engine
        assert client.get("/health").status_code == 200

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert session_module._engine is not first_engine, "重启后必须重建引擎"
