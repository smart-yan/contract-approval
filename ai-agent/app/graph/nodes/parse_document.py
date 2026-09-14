"""``parse_document`` 节点 —— 编排"把合同文件解析成结构化文档"这一步。

调用链
-----
::

    parse_document Node  →  parsers.parse_document_file()  →  DocxParser  →  ParseResult
    （编排 + State）        （选解析器 + 收敛失败）           （DOCX → 文档内容）

节点**只做三件事**：从 State 取路径与文件类型 → 调解析入口 → 把 ``ParseResult`` 写回 State。
它不出现 ``docx.Document``、不遍历 XML、不处理 ZIP —— 底层解析细节全在 Parser 里。

为什么是 async
-------------
解析库是同步阻塞的（python-docx 走 lxml，纯 CPU + 文件 IO）。按架构文档 §1.3，
阻塞调用必须离开事件循环，否则会卡住整个 Agent 服务。
因此节点声明为 ``async`` 并把解析丢进**默认线程池**。

失败如何处理
----------
不抛异常。解析失败是一个可预期的结果，由 :func:`parse_document_file` 收敛成
``ParseResult(status="FAILED")``，节点把它连同 ``error_code`` / ``error_message``
一起写进 State —— 与 ``upload_file`` 节点保持同一套失败语义。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.core.errors import AgentErrorCode
from app.graph.state import ContractReviewState
from app.parsers import parse_document_file

logger = logging.getLogger(__name__)


async def parse_document(state: ContractReviewState) -> dict[str, object]:
    """把 State 里的文件解析成结构化文档，写回 ``parse_result``。

    * 读 State：``file_path`` / ``file_type`` / ``file_id``
    * 写 State：``parse_result``；失败时额外写 ``error_code`` / ``error_message``
    """
    file_path = state.get("file_path")
    if not file_path:
        message = "缺少 Workflow 必需输入：file_path"
        logger.warning("parse_document 输入不完整 | %s", message)
        return {
            "error_code": AgentErrorCode.AGENT_INPUT_INVALID.value,
            "error_message": message,
        }

    file_type = state.get("file_type")

    # 同步阻塞的解析放线程池：不卡事件循环（架构文档 §1.3）
    result = await asyncio.to_thread(parse_document_file, Path(file_path), file_type=file_type)

    updates: dict[str, object] = {"parse_result": result}

    if result.status == "FAILED":
        logger.warning(
            "parse_document 失败 | file_id=%s type=%s code=%s message=%s",
            state.get("file_id"),
            file_type,
            result.error_code,
            result.error_message,
        )
        updates["error_code"] = result.error_code
        updates["error_message"] = result.error_message
        return updates

    logger.info(
        "parse_document 完成 | file_id=%s parser=%s status=%s paragraphs=%d chars=%d",
        state.get("file_id"),
        result.parser,
        result.status,
        len(result.paragraphs),
        len(result.text),
    )
    return updates


__all__ = ["parse_document"]
