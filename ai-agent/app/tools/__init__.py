"""Agent 调用外部系统的能力封装（当前只有 Backend 领域 API 客户端）。"""

from app.tools.backend_client import BackendClient, UploadOutcome

__all__ = ["BackendClient", "UploadOutcome"]
