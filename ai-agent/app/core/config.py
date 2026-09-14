"""AI Agent 服务配置（pydantic-settings，读仓库根目录的 .env）。

与 Backend 的关系
----------------
Agent **不访问数据库**，因此这里**没有**任何 MySQL / 连接池配置。
它对 Backend 的唯一依赖是一个 HTTP 基址：``BACKEND_BASE_URL``。

DeepSeek 配置说明
----------------
``DEEPSEEK_*`` 三项供 :class:`~app.llm.provider.DeepSeekProvider` 使用（P9-1 起）。
``/health`` 只报告"是否已配置"（``llm_configured``），**不回显密钥**；
未配置时 Provider 在本地短路，不会发出任何请求。

密钥用 ``SecretStr`` 承载，``repr()`` 与日志里恒为 ``**********``。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 本文件位于 <repo>/ai-agent/app/core/config.py
#   parents[0]=core  [1]=app  [2]=ai-agent  [3]=仓库根
# 与 Backend 共用同一个 .env（仓库根），避免两处配置漂移。
REPO_ROOT: Path = Path(__file__).resolve().parents[3]
ENV_FILE: Path = REPO_ROOT / ".env"


class AgentSettings(BaseSettings):
    """AI Agent 服务配置。字段名与 .env 中的键一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------- 应用 ---------------------------- #
    app_name: str = "合同审查 AI Agent"
    app_env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = True

    # ------------------------- Backend 依赖 ------------------------- #
    # Agent 通过 Backend 的领域 API 读写业务数据（架构裁决 C4）
    backend_base_url: str = "http://127.0.0.1:8000"
    backend_timeout_seconds: float = 60.0

    # ------------------- LLM 配置骨架（P5 不调用） ------------------- #
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = ""

    # ---------------------------- 日志 ---------------------------- #
    log_level: str = "INFO"

    # ---------------------------- 校验 ---------------------------- #
    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, v: str) -> str:
        level = v.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL 必须是 {sorted(allowed)} 之一，当前为 {v!r}")
        return level

    @field_validator("backend_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # 拼 URL 时统一不带尾斜杠，避免出现 //api/v1 这种双斜杠
        return v.strip().rstrip("/")

    # ---------------------------- 派生属性 ---------------------------- #
    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def llm_configured(self) -> bool:
        """LLM 是否已配置齐全（**只报告布尔值，绝不回显密钥**）。"""
        return bool(self.deepseek_api_key.get_secret_value()) and bool(self.deepseek_model)

    def safe_summary(self) -> dict[str, object]:
        """可安全写入日志摘要（不含任何密钥）。"""
        return {
            "app_env": self.app_env,
            "backend_base_url": self.backend_base_url,
            "backend_timeout_seconds": self.backend_timeout_seconds,
            "llm_configured": self.llm_configured,
            "deepseek_base_url": self.deepseek_base_url,
            "log_level": self.log_level,
        }


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    """返回进程内唯一的配置实例。测试可用 ``get_settings.cache_clear()`` 重置。"""
    return AgentSettings()
