"""哈希工具（架构文档 §2.1 utils/hash_utils.py、§17.1 最小核心幂等）。

两条硬性要求
------------
1. **流式读取**：计算文件 SHA-256 时按块读取，绝不把整个文件读进内存。
   上传上限是 50MB，看似不大，但并发上传多个文件时"全量读入"会直接吃掉内存；
   而且解析阶段还会处理更大的文件，流式是唯一可扩展的做法。
2. **稳定拼接**：幂等键由多个字段拼接后再哈希，**必须用明确的分隔符**，
   否则 ``("ab", "c")`` 与 ``("a", "bc")`` 会得到同一个键 —— 这是幂等键设计的经典陷阱。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

#: 流式读取的块大小（1 MiB）。兼顾系统调用次数与内存占用。
DEFAULT_CHUNK_SIZE = 1024 * 1024

#: 幂等键各段之间的分隔符。
#: 用不可能出现在业务字段里的控制字符，避免"字段内容恰好含分隔符"造成的歧义。
KEY_SEPARATOR = "\x1f"  # ASCII Unit Separator


def sha256_stream(stream: BinaryIO, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """对二进制流做流式 SHA-256，返回 64 位十六进制小写摘要。"""
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """对本地文件做流式 SHA-256。

    以 ``rb`` 打开，每次最多读 ``chunk_size`` 字节，内存占用与文件大小无关。
    """
    with path.open("rb") as fp:
        return sha256_stream(fp, chunk_size=chunk_size)


def sha256_text(text: str) -> str:
    """对文本做 SHA-256（用于构造幂等键等短输入）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_idempotency_key(*parts: str | int) -> str:
    """把若干字段拼接后哈希成幂等键（64 位十六进制，适配 ``VARCHAR(64)``）。

    分隔符见 ``KEY_SEPARATOR`` —— 保证 ``("ab", "c")`` 与 ``("a", "bc")`` 产生不同的键。
    """
    joined = KEY_SEPARATOR.join(str(part) for part in parts)
    return sha256_text(joined)
