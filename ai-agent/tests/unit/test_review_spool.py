"""``_spool_to_temp`` 的取消清理（P14-5-4）。

**W1 窗口**：文件是在这个函数里创建的 —— ``tempfile.mkstemp`` 一返回，磁盘上就已经
有一个文件了，而"写入循环"还没跑完。此时请求被取消（uvicorn 优雅关闭超时 /
部署侧的请求取消），这个刚创建的文件就没人管了。

⚠️ 为什么原来的 ``except Exception`` 抓不住：``asyncio.CancelledError`` 在 Python
3.8+ 继承自 **``BaseException``**，不是 ``Exception``。于是取消会**绕过**那段清理代码
直接向上抛 —— 这是本文件存在的全部理由。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.api.review import _spool_to_temp


def _uploads() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("agent-upload-*"))


class _CancellingUpload:
    """第一次 ``read`` 就把请求取消掉 —— 模拟"写到一半连接没了"。"""

    def __init__(self) -> None:
        self.reads = 0

    async def read(self, size: int) -> bytes:
        self.reads += 1
        raise asyncio.CancelledError()


class _FailingUpload:
    """普通异常（对照用）：这条路径**一直**是好的，不能被改动破坏。"""

    async def read(self, size: int) -> bytes:
        raise OSError("读到一半失败了")


class _OneChunkUpload:
    """给一个块就不再有数据 —— 正常路径对照。"""

    def __init__(self, chunk: bytes) -> None:
        self._chunks: list[bytes] = [chunk]

    async def read(self, size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


async def test_cancellation_during_spool_leaves_no_file_behind() -> None:
    """取消发生在**落盘过程中** → 那个刚创建的文件必须被删掉。"""
    before = _uploads()
    upload: Any = _CancellingUpload()

    with pytest.raises(asyncio.CancelledError):
        await _spool_to_temp(upload, ".docx")

    assert upload.reads == 1, "取消发生在第一次读取时"
    assert _uploads() - before == set(), "取消也必须清理掉已经创建的那个临时文件"


async def test_the_ordinary_failure_path_still_cleans_up() -> None:
    """对照：普通异常（非取消）本来就清理 —— P14-5-4 不许把它弄坏。"""
    before = _uploads()

    with pytest.raises(OSError):
        await _spool_to_temp(_FailingUpload(), ".docx")

    assert _uploads() - before == set()


async def test_a_successful_spool_returns_an_existing_file() -> None:
    """对照：正常路径返回一个**存在**的文件，并把内容写完整（清理责任交给调用方）。"""
    before = _uploads()

    path = await _spool_to_temp(_OneChunkUpload(b"hello"), ".docx")

    try:
        assert path.exists()
        assert path.read_bytes() == b"hello"
        assert _uploads() - before == {path}
    finally:
        path.unlink(missing_ok=True)
