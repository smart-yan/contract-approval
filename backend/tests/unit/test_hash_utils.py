"""哈希工具单元测试（不需要数据库）。

重点是两条**会被误用**的不变量：
1. 计算文件哈希时必须流式读取 —— 不能一次性把文件读进内存
2. 幂等键拼接必须有分隔符 —— 否则字段边界不同却哈希相同
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from app.utils.hash_utils import (
    KEY_SEPARATOR,
    build_idempotency_key,
    sha256_file,
    sha256_stream,
    sha256_text,
)


# --------------------------------------------------------------------------- #
# SHA-256 正确性
# --------------------------------------------------------------------------- #
def test_sha256_matches_hashlib_for_known_content() -> None:
    data = b"the quick brown fox"
    assert sha256_stream(io.BytesIO(data)) == hashlib.sha256(data).hexdigest()


def test_same_content_yields_same_digest(tmp_path: Path) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"identical")
    second.write_bytes(b"identical")
    assert sha256_file(first) == sha256_file(second)


def test_different_content_yields_different_digest(tmp_path: Path) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    assert sha256_file(first) != sha256_file(second)


def test_digest_is_64_hex_chars_fitting_varchar_64(tmp_path: Path) -> None:
    """``contract_file.sha256`` 是 CHAR(64) / ``idempotency_key`` 是 VARCHAR(64)。"""
    path = tmp_path / "x.bin"
    path.write_bytes(b"payload")

    digest = sha256_file(path)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


# --------------------------------------------------------------------------- #
# 流式读取（不能一次性读进内存）
# --------------------------------------------------------------------------- #
def test_sha256_file_reads_in_chunks_not_all_at_once(tmp_path: Path) -> None:
    """用一个小块大小跑大文件，断言流被**多次**读取。

    如果实现写成 ``fp.read()``，这里只会记录到 1 次读取（或读到全部字节），
    测试立即失败 —— 这正是"大文件不涨内存"的可验证形式。
    """
    size = 5 * 1024 * 1024  # 5 MiB
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * size)

    reads: list[int] = []

    class _RecordingStream(io.RawIOBase):
        def __init__(self, payload: bytes) -> None:
            self._buf = io.BytesIO(payload)

        def read(self, n: int = -1) -> bytes:  # type: ignore[override]
            chunk = self._buf.read(n)
            reads.append(len(chunk))
            return chunk

    digest = sha256_stream(_RecordingStream(path.read_bytes()), chunk_size=64 * 1024)

    assert digest == hashlib.sha256(b"x" * size).hexdigest()
    assert len(reads) > 1, "内容被一次性读完，说明不是流式计算"
    assert max(reads) <= 64 * 1024, "单次读取超过了声明的块大小"
    assert sum(reads) == size


def test_large_file_digest_is_correct(tmp_path: Path) -> None:
    size = 3 * 1024 * 1024 + 7  # 非整数块边界，验证尾部处理
    path = tmp_path / "big2.bin"
    path.write_bytes(b"a" * size)
    assert sha256_file(path, chunk_size=8192) == hashlib.sha256(b"a" * size).hexdigest()


# --------------------------------------------------------------------------- #
# 幂等键：字段边界不能产生歧义
# --------------------------------------------------------------------------- #
def test_idempotency_key_distinguishes_field_boundaries() -> None:
    """这是分隔符存在的**唯一理由**：拼接方式不同，必须得到不同的键。"""
    assert build_idempotency_key("ab", "c") != build_idempotency_key("a", "bc")
    assert build_idempotency_key("1", "23") != build_idempotency_key("12", "3")


def test_idempotency_key_is_stable_and_64_chars() -> None:
    parts = (42, "a" * 64, "v1", "ingest-v1")
    first = build_idempotency_key(*parts)
    second = build_idempotency_key(*parts)

    assert first == second, "同样输入必须得到同样的键（幂等的前提）"
    assert len(first) == 64, "必须适配 review_task.idempotency_key VARCHAR(64)"
    assert first == sha256_text(KEY_SEPARATOR.join(str(p) for p in parts))


def test_idempotency_key_accepts_int_and_str_parts() -> None:
    """``contract_id`` 是 int，其余是 str —— 混用类型不能抛异常。"""
    key = build_idempotency_key(1, "sha", "NONE", "ingest-v1")
    assert len(key) == 64


def test_idempotency_key_changes_when_any_part_changes() -> None:
    base = build_idempotency_key(1, "sha", "v1", "ingest-v1")
    assert build_idempotency_key(2, "sha", "v1", "ingest-v1") != base
    assert build_idempotency_key(1, "sha", "v2", "ingest-v1") != base
    assert build_idempotency_key(1, "sha", "NONE", "ingest-v1") != base


@pytest.mark.parametrize("bad", ["", " ", "\x00", "a\x1fb"])
def test_idempotency_key_handles_edge_content_safely(bad: str) -> None:
    """即便字段里出现了分隔符本身，也不能让两个不同输入碰撞成同一个键。"""
    assert len(build_idempotency_key(bad, "x")) == 64
    assert build_idempotency_key(bad, "x") != build_idempotency_key(bad + "x", "")
