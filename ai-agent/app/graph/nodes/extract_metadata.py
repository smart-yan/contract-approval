"""``extract_metadata`` 节点 —— 编排"从文档里读出基础元数据"这一步。

调用链
-----
::

    extract_metadata Node  →  understanding.metadata.extract_metadata()  →  list[MetadataItem]
    （State 编解码）           （纯函数：ParseResult → 元数据项）

节点**只做 State 编解码**，与 ``parse_document`` / ``identify_clauses`` 同构。

为什么保持同步、不用 asyncio.to_thread
------------------------------------
纯内存里的正则匹配，无 IO、无重载 CPU，微秒~毫秒量级 ——
架构文档 §1.3 的三档（IO / C 扩展阻塞 / 秒级 CPU）一条都不占。
"""

from __future__ import annotations

import logging

from app.graph.state import ContractReviewState
from app.understanding.metadata import extract_metadata as run_extraction

logger = logging.getLogger(__name__)


def extract_metadata(state: ContractReviewState) -> dict[str, object]:
    """把 State 里的解析结果抽出元数据，写回 ``metadata``。

    * 读 State：``parse_result``
    * 写 State：``metadata``
    """
    parse_result = state.get("parse_result")

    if parse_result is None or parse_result.status == "FAILED":
        # 没有可用的文档就抽不出东西。可预期的结果，不是错误 ——
        # 写空列表而不是抛异常，图才能正常收尾。
        logger.info(
            "extract_metadata 跳过 | file_id=%s reason=%s",
            state.get("file_id"),
            "no_parse_result" if parse_result is None else "parse_failed",
        )
        return {"metadata": []}

    items = run_extraction(parse_result)

    logger.info(
        "extract_metadata 完成 | file_id=%s fields=%s",
        state.get("file_id"),
        [item.field_key for item in items],
    )
    return {"metadata": items}


__all__ = ["extract_metadata"]
