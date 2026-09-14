"""文档解析能力域。

对外的唯一入口是 :func:`parse_document_file` —— Node 只需要调它，
不需要知道有哪些解析器、也不需要处理解析失败。

⚠️ P6-1 只有 DOCX。PDF / 扫描件 / OCR 属扩展阶段（架构文档 §17.4），
不要在这里提前加。
"""

from __future__ import annotations

from pathlib import Path

from app.core.errors import AgentErrorCode
from app.parsers.base import DocumentParseError, DocumentParser
from app.parsers.docx_parser import DocxParser
from app.schemas.document import ParseResult

#: 当前可用的解析器。
#: 新增解析器时在这里注册一行即可 —— ``resolve_parser`` 按 ``supported_file_types`` 匹配。
PARSERS: tuple[DocumentParser, ...] = (DocxParser(),)


def resolve_parser(file_type: str | None) -> DocumentParser | None:
    """按文件类型选解析器；没有能处理的返回 ``None``。

    返回 ``None`` 而不是抛异常：**"这种格式我们还不支持"是一个路由结果**，
    应当由调用方决定怎么表达，而不是让解析层替它做决定。
    """
    if not file_type:
        return None
    wanted = file_type.strip().upper()
    for parser in PARSERS:
        if wanted in parser.supported_file_types:
            return parser
    return None


def parse_document_file(path: Path, *, file_type: str | None) -> ParseResult:
    """选解析器并解析。**永远不抛异常**，失败也返回 ``ParseResult``。

    它把"选哪个解析器"与"解析失败"两件事都收敛成一个结果对象，
    于是 Node 里没有任何路由分支，也没有 try/except。
    """
    parser = resolve_parser(file_type)
    normalized_type = (file_type or "UNKNOWN").strip().upper()

    if parser is None:
        return ParseResult(
            status="FAILED",
            parser="unresolved",
            source_file_type=normalized_type,
            error_code=AgentErrorCode.PARSE_UNSUPPORTED_TYPE.value,
            error_message=f"暂时没有能解析 {normalized_type} 的 Parser",
        )

    try:
        return parser.parse(path)
    except DocumentParseError as exc:
        # Parser 内部用异常表达"我做不到"；到这里收敛成结果，
        # 上层（Node / Graph）永远只看结果
        return ParseResult(
            status="FAILED",
            parser=parser.name,
            source_file_type=normalized_type,
            error_code=exc.code.value,
            error_message=str(exc),
        )


__all__ = [
    "PARSERS",
    "DocumentParseError",
    "DocumentParser",
    "DocxParser",
    "parse_document_file",
    "resolve_parser",
]
