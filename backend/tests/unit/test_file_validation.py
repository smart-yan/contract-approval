"""文件校验单元测试（不需要数据库）。

覆盖三层校验（扩展名 / MIME / 魔数）与大小上限。
**不新增任何未裁决的业务规则** —— 断言的都是已确认的白名单行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.utils.file_utils import (
    ALLOWED_EXTENSIONS,
    MAX_UPLOAD_SIZE_BYTES,
    MIME_FALLBACK,
    validate_upload,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: 各类型的合法文件头
MAGIC = {
    "docx": b"PK\x03\x04",
    "pdf": b"%PDF-1.7\n",
    "jpg": b"\xff\xd8\xff\xe0",
    "jpeg": b"\xff\xd8\xff\xe1",
    "png": b"\x89PNG\r\n\x1a\n",
    "tif": b"II*\x00",
    "tiff": b"MM\x00*",
    "bmp": b"BM\x00\x00",
}

MIME_BY_EXT = {
    "docx": DOCX_MIME,
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "bmp": "image/bmp",
}


def _write(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


# --------------------------------------------------------------------------- #
# 白名单内的类型应当通过
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("extension", sorted(MIME_BY_EXT))
def test_allowed_types_pass_validation(tmp_path: Path, extension: str) -> None:
    payload = MAGIC[extension] + b"body"
    path = _write(tmp_path, f"sample.{extension}", payload)

    spec = validate_upload(
        filename=f"合同附件.{extension}",
        content_type=MIME_BY_EXT[extension],
        size=len(payload),
        path=path,
    )
    assert spec.extension == f".{extension}"


def test_allowed_extension_list_is_exactly_the_approved_whitelist() -> None:
    """白名单必须与架构裁决一致，防止有人悄悄放宽。"""
    assert ALLOWED_EXTENSIONS == (".bmp", ".docx", ".jpeg", ".jpg", ".pdf", ".png", ".tif", ".tiff")


def test_max_upload_size_is_50mb() -> None:
    assert MAX_UPLOAD_SIZE_BYTES == 50 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 扩展名
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", ["evil.txt", "evil.exe", "noextension", "evil.docx.exe", ""])
def test_rejects_unsupported_or_missing_extension(tmp_path: Path, filename: str) -> None:
    path = _write(tmp_path, "blob", b"%PDF-1.7\n")

    with pytest.raises(AppError) as exc_info:
        validate_upload(filename=filename, content_type="application/pdf", size=13, path=path)
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_FORMAT


def test_extension_matching_is_case_insensitive(tmp_path: Path) -> None:
    path = _write(tmp_path, "upper.PDF", MAGIC["pdf"])
    spec = validate_upload(filename="UPPER.PDF", content_type="application/pdf", size=9, path=path)
    assert spec.code == "PDF"


# --------------------------------------------------------------------------- #
# MIME 交叉校验
# --------------------------------------------------------------------------- #
def test_rejects_mime_not_matching_extension(tmp_path: Path) -> None:
    """扩展名说是 PDF，Content-Type 说是 PNG —— 典型的伪装信号。"""
    path = _write(tmp_path, "x.pdf", MAGIC["pdf"])

    with pytest.raises(AppError) as exc_info:
        validate_upload(filename="x.pdf", content_type="image/png", size=9, path=path)
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_FORMAT


def test_rejects_unknown_mime(tmp_path: Path) -> None:
    path = _write(tmp_path, "x.pdf", MAGIC["pdf"])
    with pytest.raises(AppError):
        validate_upload(filename="x.pdf", content_type="application/x-evil", size=9, path=path)


@pytest.mark.parametrize("fallback", sorted(MIME_FALLBACK))
def test_octet_stream_falls_back_to_extension(tmp_path: Path, fallback: str) -> None:
    """浏览器对 DOCX 常发 octet-stream，此时按扩展名判定（**魔数仍要过**）。"""
    payload = MAGIC["docx"] + b"body"
    path = _write(tmp_path, "x.docx", payload)

    spec = validate_upload(filename="x.docx", content_type=fallback, size=len(payload), path=path)
    assert spec.code == "DOCX"


def test_content_type_parameters_are_ignored(tmp_path: Path) -> None:
    """``application/pdf; charset=binary`` 这种带参数的形式要能被正确解析。"""
    path = _write(tmp_path, "x.pdf", MAGIC["pdf"])
    spec = validate_upload(
        filename="x.pdf", content_type="application/pdf; charset=binary", size=9, path=path
    )
    assert spec.code == "PDF"


# --------------------------------------------------------------------------- #
# 魔数（最权威的一层）
# --------------------------------------------------------------------------- #
def test_rejects_content_not_matching_extension(tmp_path: Path) -> None:
    """把纯文本改名成 .pdf —— 扩展名与 MIME 都能伪造，魔数不能。"""
    path = _write(tmp_path, "fake.pdf", b"this is definitely not a pdf")

    with pytest.raises(AppError) as exc_info:
        validate_upload(filename="fake.pdf", content_type="application/pdf", size=27, path=path)
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_FORMAT


def test_octet_stream_still_requires_valid_magic(tmp_path: Path) -> None:
    """兜底规则只放宽 MIME，**不放宽魔数**。"""
    path = _write(tmp_path, "fake.docx", b"plain text pretending to be docx")

    with pytest.raises(AppError) as exc_info:
        validate_upload(filename="fake.docx", content_type="application/octet-stream", size=33, path=path)
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_FORMAT


def test_tiff_accepts_both_byte_orders(tmp_path: Path) -> None:
    for index, payload in enumerate((b"II*\x00rest", b"MM\x00*rest")):
        path = _write(tmp_path, f"t{index}.tif", payload)
        spec = validate_upload(
            filename=f"t{index}.tif", content_type="image/tiff", size=len(payload), path=path
        )
        assert spec.code == "TIFF"


# --------------------------------------------------------------------------- #
# 大小与空文件
# --------------------------------------------------------------------------- #
def test_rejects_empty_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "empty.pdf", b"")
    with pytest.raises(AppError) as exc_info:
        validate_upload(filename="empty.pdf", content_type="application/pdf", size=0, path=path)
    assert exc_info.value.code == ErrorCode.FILE_CORRUPTED


def test_rejects_oversize_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "big.pdf", MAGIC["pdf"])

    with pytest.raises(AppError) as exc_info:
        validate_upload(
            filename="big.pdf",
            content_type="application/pdf",
            size=MAX_UPLOAD_SIZE_BYTES + 1,
            path=path,
        )
    assert exc_info.value.code == ErrorCode.FILE_TOO_LARGE


def test_accepts_file_exactly_at_the_limit(tmp_path: Path) -> None:
    """边界值：正好等于上限应当通过（不是 > 上限）。"""
    path = _write(tmp_path, "limit.pdf", MAGIC["pdf"])
    spec = validate_upload(
        filename="limit.pdf",
        content_type="application/pdf",
        size=MAX_UPLOAD_SIZE_BYTES,
        path=path,
    )
    assert spec.code == "PDF"


def test_size_check_precedes_extension_check(tmp_path: Path) -> None:
    """超大且扩展名非法时，应优先报"太大"——避免先泄露白名单信息。"""
    path = _write(tmp_path, "blob", b"x")

    with pytest.raises(AppError) as exc_info:
        validate_upload(
            filename="evil.exe",
            content_type="application/octet-stream",
            size=MAX_UPLOAD_SIZE_BYTES + 1,
            path=path,
        )
    assert exc_info.value.code == ErrorCode.FILE_TOO_LARGE
