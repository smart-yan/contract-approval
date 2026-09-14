"""存储后端单元测试（不需要数据库）。

覆盖路径安全与本地落位语义。这些都是**纯本地逻辑**，
用 ``tmp_path`` 即可，不必牵扯 MySQL。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.storage.base import normalize_key
from app.storage.local import LocalStorageBackend


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorageBackend:
    return LocalStorageBackend(tmp_path / "storage")


# --------------------------------------------------------------------------- #
# storage_key 路径安全
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_key",
    [
        "",  # 空
        "   ",  # 全空白
        "/etc/passwd",  # 绝对路径
        "C:/Windows/system32",  # 盘符
        "C:\\Windows",  # 盘符 + 反斜杠
        "../outside",  # 路径穿越
        "uploads/../../outside",  # 藏在中间的穿越
        "uploads\\..\\outside",  # 反斜杠形态的穿越
        "a/b\\c",  # 混用分隔符
    ],
)
def test_normalize_key_rejects_unsafe_keys(bad_key: str) -> None:
    with pytest.raises(AppError) as exc_info:
        normalize_key(bad_key)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("uploads/1/abc.docx", "uploads/1/abc.docx"),
        ("uploads//1//abc.docx", "uploads/1/abc.docx"),  # 折叠重复分隔符
        ("./uploads/1/abc.docx", "uploads/1/abc.docx"),  # 去掉当前目录段
        ("uploads/./1/abc.docx", "uploads/1/abc.docx"),
    ],
)
def test_normalize_key_accepts_and_normalizes_safe_keys(raw: str, expected: str) -> None:
    assert normalize_key(raw) == expected


def test_resolve_rejects_traversal_even_if_it_somehow_reaches_resolve(
    storage: LocalStorageBackend,
) -> None:
    """``resolve`` 自身也有一道物理越界复检，不只依赖字符串校验。"""
    with pytest.raises(AppError):
        storage.resolve("../../etc/passwd")


def test_build_upload_key_does_not_use_user_filename(storage: LocalStorageBackend) -> None:
    """路径只由 contract_id + sha256 + 扩展名组成，用户文件名不参与。"""
    key = storage.build_upload_key(contract_id=42, sha256="a" * 64, extension=".docx")
    assert key == f"uploads/42/{'a' * 64}.docx"
    assert ".." not in key


def test_build_upload_key_requires_dotted_extension(storage: LocalStorageBackend) -> None:
    with pytest.raises(AppError):
        storage.build_upload_key(contract_id=1, sha256="b" * 64, extension="docx")


def test_build_key_rejects_unsafe_parts(storage: LocalStorageBackend) -> None:
    with pytest.raises(AppError):
        storage.build_key("uploads", "..", "x.docx")


# --------------------------------------------------------------------------- #
# 落位 / 读取 / 存在 / 删除
# --------------------------------------------------------------------------- #
def test_save_moves_file_atomically_and_removes_source(storage: LocalStorageBackend, tmp_path: Path) -> None:
    source = storage.new_temp_path(suffix=".docx")
    source.write_bytes(b"PK\x03\x04payload")
    assert source.exists()

    key = storage.build_upload_key(contract_id=7, sha256="c" * 64, extension=".docx")
    storage.save(key=key, source=source)

    assert storage.exists(key) is True
    assert not source.exists(), "save 应当是移动（os.replace）而不是复制，源文件必须消失"
    assert storage.resolve(key).read_bytes() == b"PK\x03\x04payload"


def test_save_creates_missing_directories(storage: LocalStorageBackend) -> None:
    source = storage.new_temp_path(suffix=".pdf")
    source.write_bytes(b"%PDF-1.7")
    key = storage.build_upload_key(contract_id=999, sha256="d" * 64, extension=".pdf")

    storage.save(key=key, source=source)
    assert storage.resolve(key).is_file()


def test_open_reads_back_exact_bytes(storage: LocalStorageBackend) -> None:
    payload = b"%PDF-1.7\nstream"
    source = storage.new_temp_path(suffix=".pdf")
    source.write_bytes(payload)
    key = storage.build_upload_key(contract_id=1, sha256="e" * 64, extension=".pdf")
    storage.save(key=key, source=source)

    with storage.open(key) as fp:
        assert fp.read() == payload


def test_open_missing_file_raises_file_not_found(storage: LocalStorageBackend) -> None:
    key = storage.build_upload_key(contract_id=1, sha256="f" * 64, extension=".docx")
    with pytest.raises(AppError) as exc_info:
        storage.open(key)
    assert exc_info.value.code == ErrorCode.FILE_NOT_FOUND


def test_exists_false_for_missing_and_for_illegal_key(storage: LocalStorageBackend) -> None:
    assert storage.exists("uploads/1/nope.docx") is False
    # 非法 key 按"不存在"处理，不应把校验错误升级成 500
    assert storage.exists("../../etc/passwd") is False


def test_delete_returns_true_then_false(storage: LocalStorageBackend) -> None:
    source = storage.new_temp_path(suffix=".docx")
    source.write_bytes(b"PK\x03\x04x")
    key = storage.build_upload_key(contract_id=3, sha256="1" * 64, extension=".docx")
    storage.save(key=key, source=source)

    assert storage.delete(key) is True
    assert storage.exists(key) is False
    assert storage.delete(key) is False, "重复删除应返回 False 而不是抛异常"


# --------------------------------------------------------------------------- #
# 临时文件
# --------------------------------------------------------------------------- #
def test_new_temp_path_is_unique_and_under_temp_dir(storage: LocalStorageBackend) -> None:
    first = storage.new_temp_path(suffix=".docx")
    second = storage.new_temp_path(suffix=".docx")

    assert first != second
    assert first.parent == storage.root / "tmp"
    assert first.parent.is_dir(), "临时目录应当被自动创建"


def test_discard_temp_is_safe_for_missing_file(storage: LocalStorageBackend) -> None:
    missing = storage.root / "tmp" / "does-not-exist"
    storage.discard_temp(missing)  # 不应抛异常


# --------------------------------------------------------------------------- #
# 存储异常
# --------------------------------------------------------------------------- #
def test_save_failure_surfaces_as_storage_unavailable(tmp_path: Path) -> None:
    """底层 OSError 必须收敛为统一的存储错误码，而不是裸奔到 500。"""
    storage = LocalStorageBackend(tmp_path / "storage")
    missing_source = tmp_path / "not-there.docx"  # 故意不存在
    key = storage.build_upload_key(contract_id=1, sha256="2" * 64, extension=".docx")

    with pytest.raises(AppError) as exc_info:
        storage.save(key=key, source=missing_source)
    assert exc_info.value.code == ErrorCode.STORAGE_UNAVAILABLE


def test_storage_error_message_does_not_leak_absolute_path(tmp_path: Path) -> None:
    """错误信息里不能出现服务器绝对路径。"""
    storage = LocalStorageBackend(tmp_path / "storage")
    key = storage.build_upload_key(contract_id=1, sha256="3" * 64, extension=".docx")

    with pytest.raises(AppError) as exc_info:
        storage.save(key=key, source=tmp_path / "missing.docx")
    assert str(tmp_path) not in exc_info.value.message
