"""``extract_keywords`` 节点 —— 编排"扫出文档里的主题词"这一步。

调用链
-----
::

    extract_keywords Node  →  understanding.keywords.extract_keywords()  →  list[KeywordHit]
    （State 编解码）           （纯函数：ParseResult → 命中列表）

节点**只做 State 编解码**，与 ``parse_document`` / ``identify_clauses`` /
``extract_metadata`` 同构。关键词扫描是纯内存的字符串查找，同步即可。
"""

from __future__ import annotations

import logging

from app.graph.state import ContractReviewState
from app.understanding.keywords import extract_keywords as run_extraction

logger = logging.getLogger(__name__)


def extract_keywords(state: ContractReviewState) -> dict[str, object]:
    """把 State 里的解析结果扫出主题词，写回 ``keywords``。

    * 读 State：``parse_result``
    * 写 State：``keywords``
    """
    parse_result = state.get("parse_result")

    if parse_result is None or parse_result.status == "FAILED":
        logger.info(
            "extract_keywords 跳过 | file_id=%s reason=%s",
            state.get("file_id"),
            "no_parse_result" if parse_result is None else "parse_failed",
        )
        return {"keywords": []}

    hits = run_extraction(parse_result)

    logger.info(
        "extract_keywords 完成 | file_id=%s hits=%d terms=%d",
        state.get("file_id"),
        len(hits),
        len({hit.term for hit in hits}),
    )
    return {"keywords": hits}


__all__ = ["extract_keywords"]
