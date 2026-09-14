"""文件存储抽象（架构文档 §2.1、§17 P4）。

为什么要抽象
------------
P4 只需要本地磁盘存储，但"存到哪里"是可替换的能力：将来可能换成
对象存储（S3/OSS/MinIO）或共享网络盘。把接口固定下来，业务代码只依赖
``StorageBackend``，替换后端时不需要改 Service。

``storage_key`` 的语义
---------------------
**key 是后端内部的、受控的相对标识**，不是用户文件名，也不是绝对路径。
本模块的 :func:`normalize_key` 是唯一的合法性入口：

* 拒绝绝对路径（``/etc/passwd``、``C:\\x``）
* 拒绝路径穿越（``..``）
* 拒绝反斜杠（Windows 分隔符走私）
* 拒绝空 key

所有实现都必须先过这一关，避免"用户可控字符串直接拼路径"这一类漏洞。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from app.core.errors import AppError, ErrorCode


def normalize_key(key: str | None) -> str:
    """校验并规范化 ``storage_key``；非法时抛 :class:`AppError`。

    规范化后的 key 一定满足：相对路径、以 ``/`` 分隔、不含 ``.`` / ``..`` 段。
    """
    if not key or not key.strip():
        raise AppError(code=ErrorCode.VALIDATION_ERROR, message="storage_key 不能为空")

    raw = key.strip()

    # 反斜杠在 Linux 上是合法文件名字符，但在 Windows 上是路径分隔符。
    # 一律拒绝，避免同一份 key 在不同平台上解析出不同的路径。
    if "\\" in raw:
        raise AppError(
            code=ErrorCode.VALIDATION_ERROR,
            message="storage_key 不允许包含反斜杠",
        )

    if raw.startswith("/"):
        raise AppError(code=ErrorCode.VALIDATION_ERROR, message="storage_key 不允许是绝对路径")

    # Windows 盘符（C:...）与 UNC（//server）在 startswith("/") 之外还需单独拦一次
    if len(raw) >= 2 and raw[1] == ":":
        raise AppError(code=ErrorCode.VALIDATION_ERROR, message="storage_key 不允许包含盘符")

    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts:
        raise AppError(code=ErrorCode.VALIDATION_ERROR, message="storage_key 不含有效路径段")

    if any(part == ".." for part in parts):
        raise AppError(code=ErrorCode.VALIDATION_ERROR, message="storage_key 不允许包含 .. 路径段")

    return "/".join(parts)


class StorageBackend(ABC):
    """文件存储后端接口。

    实现约定：所有方法都是**同步阻塞**的，调用方负责放到线程池里执行
    （见架构文档 §1.3 的并发模型）。
    """

    @abstractmethod
    def build_key(self, *parts: str | int) -> str:
        """按后端约定拼出一个合法的 ``storage_key``。"""

    @abstractmethod
    def save(self, *, key: str, source: Path) -> None:
        """把 ``source`` 文件**原子地**移动到该 key 对应的位置。

        原子性很重要：不能出现"文件写到一半就被读到"的中间态。
        """

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """以二进制只读方式打开该 key 对应的文件。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """判断该 key 对应的文件是否存在。"""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """删除该 key 对应的文件。返回是否真的删掉了（不存在时返回 False）。"""

    @abstractmethod
    def resolve(self, key: str) -> Path:
        """把 key 解析成本后端的真实路径（仅供后端内部与测试使用）。

        ⚠️ 该路径**不得**返回给客户端 —— 会泄露服务器目录结构。
        """
