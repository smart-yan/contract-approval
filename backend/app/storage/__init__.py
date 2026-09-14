"""文件存储能力域（架构文档 §2.1、§17 P4）。

对外只暴露抽象与默认实现，业务代码依赖 :class:`StorageBackend` 而不是具体后端：

    from app.storage import StorageBackend, get_storage

将来接入对象存储时，新增一个 ``S3StorageBackend`` 并调整 :func:`get_storage`
的返回即可，Service 层无需改动。
"""

from __future__ import annotations

from app.storage.base import StorageBackend, normalize_key
from app.storage.local import LocalStorageBackend, get_storage, reset_storage

__all__ = [
    "LocalStorageBackend",
    "StorageBackend",
    "get_storage",
    "normalize_key",
    "reset_storage",
]
