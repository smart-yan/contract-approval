"""文件类型与大小校验（架构文档 §10.1、§17.1）。

**所有类型判断集中在本模块**，不得散落到 API 层或 Service 层 ——
白名单只有一处定义，改规则时不会漏改。

三层校验，缺一不可
------------------
1. **扩展名**：用户文件名的后缀
2. **MIME**：客户端声明的 Content-Type
3. **魔数（magic number）**：文件头字节

为什么不能只信扩展名：把 `evil.exe` 改名成 `a.pdf` 就能绕过。
为什么不能只信 MIME：客户端可以任意伪造 Content-Type。
因此**以魔数为准**，扩展名与 MIME 只用于"选哪个白名单条目"。

唯一放宽的地方：浏览器对 DOCX 常发 `application/octet-stream`（不认识的二进制），
此时允许**退回按扩展名判定**（见 ``MIME_FALLBACK``）—— 但魔数校验仍然照做。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AppError, ErrorCode

#: 上传大小上限（架构文档 §10.1：50MB）
MAX_UPLOAD_SIZE_BYTES: int = 50 * 1024 * 1024

#: 魔数嗅探需要读取的头部字节数
_MAGIC_READ_SIZE = 16

#: 客户端无法给出准确类型时的兜底 MIME。命中此值时退回按扩展名判定。
MIME_FALLBACK: frozenset[str] = frozenset({"application/octet-stream", "binary/octet-stream", ""})


@dataclass(frozen=True, slots=True)
class FileTypeSpec:
    """一种允许上传的文件类型。

    ``magic_prefixes`` 是文件头可能的字节序列（任一命中即可）。
    """

    code: str
    extension: str
    mime_types: frozenset[str]
    magic_prefixes: tuple[bytes, ...]


#: 允许上传的文件类型（架构文档 §17 P4：DOCX / PDF / 常见图片格式）
ALLOWED_FILE_TYPES: tuple[FileTypeSpec, ...] = (
    FileTypeSpec(
        code="DOCX",
        extension=".docx",
        mime_types=frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
        # DOCX 是 ZIP 容器，因此魔数即 ZIP 头。
        # ⚠️ 这只能证明"是个 zip"，不能证明"是合法 DOCX"；完整的 OOXML 校验属于 P7 解析阶段。
        magic_prefixes=(b"PK\x03\x04",),
    ),
    FileTypeSpec(
        code="PDF",
        extension=".pdf",
        mime_types=frozenset({"application/pdf", "application/x-pdf"}),
        magic_prefixes=(b"%PDF-",),
    ),
    FileTypeSpec(
        code="JPEG",
        extension=".jpg",
        mime_types=frozenset({"image/jpeg", "image/jpg"}),
        magic_prefixes=(b"\xff\xd8\xff",),
    ),
    FileTypeSpec(
        code="JPEG",
        extension=".jpeg",
        mime_types=frozenset({"image/jpeg", "image/jpg"}),
        magic_prefixes=(b"\xff\xd8\xff",),
    ),
    FileTypeSpec(
        code="PNG",
        extension=".png",
        mime_types=frozenset({"image/png"}),
        magic_prefixes=(b"\x89PNG\r\n\x1a\n",),
    ),
    FileTypeSpec(
        code="TIFF",
        extension=".tif",
        mime_types=frozenset({"image/tiff"}),
        # TIFF 有两种字节序
        magic_prefixes=(b"II*\x00", b"MM\x00*"),
    ),
    FileTypeSpec(
        code="TIFF",
        extension=".tiff",
        mime_types=frozenset({"image/tiff"}),
        magic_prefixes=(b"II*\x00", b"MM\x00*"),
    ),
    FileTypeSpec(
        code="BMP",
        extension=".bmp",
        mime_types=frozenset({"image/bmp", "image/x-ms-bmp"}),
        magic_prefixes=(b"BM",),
    ),
)

_BY_EXTENSION: dict[str, FileTypeSpec] = {spec.extension: spec for spec in ALLOWED_FILE_TYPES}
_BY_MIME: dict[str, FileTypeSpec] = {mime: spec for spec in ALLOWED_FILE_TYPES for mime in spec.mime_types}

#: 供错误信息展示的允许扩展名列表
ALLOWED_EXTENSIONS: tuple[str, ...] = tuple(sorted(_BY_EXTENSION))


def normalize_extension(filename: str | None) -> str:
    """取小写扩展名（含点）。无扩展名时返回空串。"""
    if not filename:
        return ""
    return Path(filename).suffix.lower()


def sniff_magic(path: Path) -> bytes:
    """读取文件头部若干字节（不加载整个文件）。"""
    with path.open("rb") as fp:
        return fp.read(_MAGIC_READ_SIZE)


def type_code_for_extension(extension: str | None) -> str | None:
    """由扩展名反推文件类型码（如 ``.docx`` → ``DOCX``）。

    ``contract_file`` 表只存了 ``file_ext``、没有单独的类型码列，
    因此"复用已有附件"时需要靠它还原出上传时判定出的类型 ——
    与上传走同一份白名单，保证两处口径一致。
    """
    if not extension:
        return None
    normalized = extension.lower() if extension.startswith(".") else f".{extension.lower()}"
    spec = _BY_EXTENSION.get(normalized)
    return spec.code if spec else None


def _matches_magic(header: bytes, spec: FileTypeSpec) -> bool:
    return any(header.startswith(prefix) for prefix in spec.magic_prefixes)


def validate_upload(
    *,
    filename: str | None,
    content_type: str | None,
    size: int,
    path: Path,
) -> FileTypeSpec:
    """三层校验，通过则返回命中的 :class:`FileTypeSpec`，否则抛 :class:`AppError`。

    :param size: 实际写入磁盘的字节数（不是 Content-Length，后者可被伪造）
    :param path: 已落盘的临时文件，用于魔数嗅探
    """
    # ---- 1. 大小 ----
    if size <= 0:
        raise AppError(code=ErrorCode.FILE_CORRUPTED, message="上传文件为空")
    if size > MAX_UPLOAD_SIZE_BYTES:
        raise AppError(
            code=ErrorCode.FILE_TOO_LARGE,
            message=f"文件大小 {size} 字节超过上限 {MAX_UPLOAD_SIZE_BYTES} 字节",
            details={"size": size, "max_size": MAX_UPLOAD_SIZE_BYTES},
        )

    # ---- 2. 扩展名 ----
    extension = normalize_extension(filename)
    spec = _BY_EXTENSION.get(extension)
    if spec is None:
        raise AppError(
            code=ErrorCode.UNSUPPORTED_FORMAT,
            message=f"不支持的文件扩展名：{extension or '(无)'}",
            details={"allowed_extensions": list(ALLOWED_EXTENSIONS)},
        )

    # ---- 3. MIME（与扩展名交叉验证；octet-stream 等兜底值除外）----
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime not in MIME_FALLBACK:
        mime_spec = _BY_MIME.get(mime)
        if mime_spec is None or mime_spec.code != spec.code:
            raise AppError(
                code=ErrorCode.UNSUPPORTED_FORMAT,
                message=f"文件类型与扩展名不一致：扩展名 {extension} 对应 {spec.code}，"
                f"而 Content-Type 为 {mime or '(空)'}",
                details={"extension": extension, "content_type": mime},
            )

    # ---- 4. 魔数（最权威的一层）----
    header = sniff_magic(path)
    if not _matches_magic(header, spec):
        raise AppError(
            code=ErrorCode.UNSUPPORTED_FORMAT,
            message=f"文件内容与扩展名不符：{extension} 的文件头不是合法的 {spec.code}",
            details={"extension": extension, "expected": spec.code},
        )

    return spec
