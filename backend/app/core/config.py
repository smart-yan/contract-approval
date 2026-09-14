"""应用配置基座。

架构文档：§2.1 core/config.py（pydantic-settings，读 .env）、§17.2（P2 需交付 DB 连接池配置）。

设计要点
--------
1. **路径无关**：`.env` 位置由本文件反推仓库根目录得到，不依赖进程工作目录
   （在 PyCharm 里以 backend/ 为 cwd、或在仓库根目录运行，结果一致）。
2. **密码不进日志**：``mysql_password`` 使用 ``SecretStr``，``repr()`` / 日志中恒为 ``**********``。
   需要拼接 DSN 时显式调用 ``.get_secret_value()``；需要对外展示时用 ``database_url_safe``。
3. **单一来源**：全项目只通过 ``get_settings()`` 获取配置（带缓存），禁止各处自行读 os.environ。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
# 本文件位于 <repo>/backend/app/core/config.py
#   parents[0] = core   parents[1] = app   parents[2] = backend   parents[3] = 仓库根
REPO_ROOT: Path = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE: Path = REPO_ROOT / ".env"

# 允许用环境变量覆盖 .env 位置（测试 / 部署时有用）
ENV_FILE: str = os.getenv("ENV_FILE", str(DEFAULT_ENV_FILE))


class Settings(BaseSettings):
    """全局配置。字段名与 .env 中的键一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------- 应用 ---------------------------- #
    app_name: str = "合同审批审查系统"
    app_env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"

    # ---------------------------- MySQL ---------------------------- #
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: SecretStr = SecretStr("")
    mysql_db: str = "contract_approval"

    # ------------------------- 连接池（§17.2）------------------------- #
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 3600  # 秒；小于 MySQL wait_timeout，避免用到已被服务端关闭的连接
    db_pool_timeout: int = 30  # 秒；取连接的最长等待时间
    db_echo: bool = False  # True 时打印所有 SQL，仅本地排查用

    # ---------------------------- 日志 ---------------------------- #
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ------------------ 并发模型基座（架构文档 §1.3）------------------ #
    # 同步阻塞 I/O（python-docx / PyMuPDF 等 C 扩展）走的默认线程池大小
    thread_pool_size: int = 16

    # OCR 执行器模式：process = 独立进程池（默认）；thread = 线程池（实测更优时可切换）
    ocr_executor_mode: Literal["process", "thread"] = "process"

    # OCR 进程池大小。§1.3 规定：默认 1，允许 1 / 2 / 4，
    # 以避免 `Worker 数 × 进程池大小` 造成进程爆炸。
    ocr_max_workers: int = 1

    # ---------------------------- 校验 ---------------------------- #
    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, v: str) -> str:
        level = v.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL 必须是 {sorted(allowed)} 之一，当前为 {v!r}")
        return level

    @field_validator("ocr_max_workers")
    @classmethod
    def _check_ocr_max_workers(cls, v: int) -> int:
        # 架构文档 §1.3：「OCR_MAX_WORKERS 默认 1，可配置 1 / 2 / 4」
        if v not in (1, 2, 4):
            raise ValueError(f"OCR_MAX_WORKERS 只允许 1 / 2 / 4，当前为 {v}")
        return v

    @field_validator("db_pool_size", "db_max_overflow")
    @classmethod
    def _check_pool_size(cls, v: int) -> int:
        if v < 0:
            raise ValueError("连接池参数不允许为负数")
        return v

    # ---------------------------- 派生属性 ---------------------------- #
    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    def _dsn(self, driver: str, password: str, *, mask_password: bool = False) -> str:
        """拼装 DSN。密码做 URL 编码，避免含 @ / : / / 等字符时解析错位。

        ``mask_password=True`` 时用固定掩码替换密码，**且不参与 URL 编码**
        （``quote_plus`` 会把 ``*`` 编码成 ``%2A``，导致掩码不可读）。
        """
        user = quote_plus(self.mysql_user)
        if not password:
            auth = user
        else:
            auth = f"{user}:{'******' if mask_password else quote_plus(password)}"
        return f"mysql+{driver}://{auth}@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"

    @property
    def database_url(self) -> str:
        """异步 DSN（aiomysql），供 SQLAlchemy async engine 使用。**含明文密码，禁止打印。**"""
        return self._dsn("aiomysql", self.mysql_password.get_secret_value())

    @property
    def database_url_sync(self) -> str:
        """同步 DSN（pymysql），供 Alembic 离线模式 / 运维脚本使用。**含明文密码，禁止打印。**"""
        return self._dsn("pymysql", self.mysql_password.get_secret_value())

    @property
    def database_url_safe(self) -> str:
        """脱敏 DSN，可安全写入日志与错误信息。"""
        return self._dsn("aiomysql", self.mysql_password.get_secret_value(), mask_password=True)

    def safe_summary(self) -> dict[str, Any]:
        """可安全记录到日志的配置摘要（不含任何密钥）。"""
        return {
            "app_env": self.app_env,
            "debug": self.debug,
            "api_v1_prefix": self.api_v1_prefix,
            "database": self.database_url_safe,
            "db_pool_size": self.db_pool_size,
            "db_max_overflow": self.db_max_overflow,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "thread_pool_size": self.thread_pool_size,
            "ocr_executor_mode": self.ocr_executor_mode,
            "ocr_max_workers": self.ocr_max_workers,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回全局唯一配置实例（进程内缓存）。

    需要重新加载时调用 ``get_settings.cache_clear()``（测试用）。
    """
    return Settings()
