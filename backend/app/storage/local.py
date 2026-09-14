"""本地磁盘存储后端（架构文档 §7.2 ``contract_file.storage_path``）。

目录结构遵循 §7.2 的规定：

```
backend/storage/
├─ uploads/{contract_id}/{sha256}.{ext}   # 原始合同附件（P4）
├─ renders/                                # OCR 页面图 / 报告产物（P7 起）
├─ reports/                                # 导出报告（P12 起）
└─ tmp/                                    # 上传中转（同一卷，保证 os.replace 原子）
```

为什么要有 ``tmp/``
------------------
上传流程是「先落临时文件 → 算哈希 → 校验 → 原子移动」。
``os.replace`` 只在**同一文件系统内**才是原子的，跨卷会退化成"复制+删除"，
中途失败会留下半个文件。把临时目录放在 ``storage/`` 下方（同一卷），
就保证了移动的原子性。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import BinaryIO

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.storage.base import StorageBackend, normalize_key

logger = get_logger(__name__)

#: 后端默认根目录：<repo>/backend/storage
#: local.py 位于 backend/app/storage/local.py → parents[0]=storage, [1]=app, [2]=backend
BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_STORAGE_ROOT = BACKEND_DIR / "storage"

#: 临时中转目录名（与 uploads/ 同一卷）
TEMP_DIR_NAME = "tmp"

#: 上传文件的相对目录（§7.2：storage/uploads/{contract_id}/）
UPLOADS_DIR_NAME = "uploads"


class LocalStorageBackend(StorageBackend):
    """把文件存放在本地磁盘的指定根目录下。"""

    def __init__(self, root: Path, *, temp_dir_name: str = TEMP_DIR_NAME) -> None:
        self._root = Path(root).resolve()
        self._temp_root = self._root / temp_dir_name

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Path:
        """存储根目录。**不要返回给客户端。**"""
        return self._root

    # ------------------------------------------------------------------ #
    # key 构造与解析
    # ------------------------------------------------------------------ #
    def build_key(self, *parts: str | int) -> str:
        """按 ``a/b/c`` 形式拼 key；任一段含非法字符时抛出异常。"""
        if not parts:
            raise AppError(code=ErrorCode.VALIDATION_ERROR, message="build_key 至少需要一个路径段")
        return normalize_key("/".join(str(part) for part in parts))

    def build_upload_key(self, *, contract_id: int, sha256: str, extension: str) -> str:
        """上传附件的 key：``uploads/{contract_id}/{sha256}{ext}``（§7.2）。

        刻意**不使用用户原始文件名**做路径 —— 那会让用户可控字符串进入文件系统路径。
        原始文件名作为 metadata 存在 ``contract_file.file_name`` 里。
        """
        if not extension.startswith("."):
            raise AppError(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"扩展名必须以点开头，收到 {extension!r}",
            )
        return self.build_key(UPLOADS_DIR_NAME, contract_id, f"{sha256}{extension}")

    def resolve(self, key: str) -> Path:
        """解析为绝对路径，并做**最终越界检查**。

        ``normalize_key`` 已在字符串层面拦掉了 ``..`` 与绝对路径；
        这里再做一次解析后的物理校验，防御符号链接等间接逃逸。
        """
        safe_key = normalize_key(key)
        candidate = (self._root / safe_key).resolve()

        if candidate != self._root and self._root not in candidate.parents:
            raise AppError(
                code=ErrorCode.VALIDATION_ERROR,
                message="storage_key 解析后越出存储根目录",
            )
        return candidate

    # ------------------------------------------------------------------ #
    # 临时文件
    # ------------------------------------------------------------------ #
    def new_temp_path(self, *, suffix: str = "") -> Path:
        """返回 ``storage/tmp/`` 下一个**未被占用**的唯一路径（仅建目录，不建文件）。

        文件由调用方打开写入。放在同一卷是为了让后续的 :meth:`save`
        能用 ``os.replace`` 原子落位。
        """
        try:
            self._temp_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise self._storage_error("创建临时目录失败", exc) from None
        return self._temp_root / f"{uuid.uuid4().hex}{suffix}"

    def discard_temp(self, path: Path) -> None:
        """尽力删除临时文件。**失败只记日志，不抛异常**（清理不能掩盖主流程的错误）。"""
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "临时文件清理失败",
                extra={"error_type": type(exc).__name__, "temp_name": path.name},
            )

    # ------------------------------------------------------------------ #
    # StorageBackend 实现
    # ------------------------------------------------------------------ #
    def save(self, *, key: str, source: Path) -> None:
        target = self.resolve(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 同卷内 os.replace 是原子操作：要么旧文件在，要么新文件在，不存在中间态
            os.replace(source, target)
        except OSError as exc:
            raise self._storage_error("写入文件失败", exc) from None

    def open(self, key: str) -> BinaryIO:
        target = self.resolve(key)
        try:
            return target.open("rb")
        except FileNotFoundError:
            raise AppError(
                code=ErrorCode.FILE_NOT_FOUND,
                message="文件不存在",
            ) from None
        except OSError as exc:
            raise self._storage_error("读取文件失败", exc) from None

    def exists(self, key: str) -> bool:
        try:
            return self.resolve(key).is_file()
        except AppError:
            # key 非法时按"不存在"处理，避免把校验错误泄露成 500
            return False

    def delete(self, key: str) -> bool:
        target = self.resolve(key)
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise self._storage_error("删除文件失败", exc) from None

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _storage_error(action: str, exc: OSError) -> AppError:
        """把底层 OSError 收敛成统一的存储错误。

        ⚠️ 只记录异常类型，不把 ``exc`` 的原文塞进 message ——
        OSError 的字符串里常含**服务器绝对路径**，不应回传给客户端。
        """
        logger.warning(
            action,
            extra={"error_type": type(exc).__name__, "errno": exc.errno},
        )
        return AppError(
            code=ErrorCode.STORAGE_UNAVAILABLE,
            message=f"{action}（{type(exc).__name__}）",
        )


# --------------------------------------------------------------------------- #
# 默认实例
# --------------------------------------------------------------------------- #
_default_backend: LocalStorageBackend | None = None


def get_storage() -> LocalStorageBackend:
    """返回进程内唯一的默认存储后端（懒加载）。

    测试应自行构造 ``LocalStorageBackend(tmp_path)`` 注入，
    而不是改这个全局实例 —— 那样会污染其它用例。
    """
    global _default_backend
    if _default_backend is None:
        _default_backend = LocalStorageBackend(DEFAULT_STORAGE_ROOT)
    return _default_backend


def reset_storage() -> None:
    """清空默认实例（测试用）。"""
    global _default_backend
    _default_backend = None


__all__ = [
    "BACKEND_DIR",
    "DEFAULT_STORAGE_ROOT",
    "UPLOADS_DIR_NAME",
    "LocalStorageBackend",
    "get_storage",
    "reset_storage",
]
