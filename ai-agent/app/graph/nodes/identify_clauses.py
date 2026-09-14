"""``identify_clauses`` 节点 —— 编排"把文档切成条款"这一步。

调用链
-----
::

    identify_clauses Node  →  understanding.clauses.identify_clauses()  →  list[Clause]
    （State 编解码）           （纯函数：ParseResult → 条款列表）

节点**只做 State 编解码**，与 ``parse_document`` / ``upload_file`` 同构。

为什么保持同步、不用 asyncio.to_thread
------------------------------------
架构文档 §1.3 的三档分法是按 **IO / C 扩展阻塞 / 秒级 CPU** 划分的。
条款切分是纯内存里的轻量正则匹配，**一条都不占** —— 几十到几千个短字符串的匹配
在微秒~毫秒量级。为了"写法统一"套线程池，是拿线程切换开销换一个不存在的收益。
将来若正则变重，改成 ``async def`` 再包 ``to_thread`` 只需三行，契约不变。

失败如何处理
----------
不抛异常。没有可用的解析结果时写 ``clauses = []``，由下游判断 ——
与 ``parse_document`` / ``upload_file`` 保持同一套失败语义。
"""

from __future__ import annotations

import logging

from app.graph.state import ContractReviewState
from app.understanding.clauses import identify_clauses as build_clauses

logger = logging.getLogger(__name__)


def identify_clauses(state: ContractReviewState) -> dict[str, object]:
    """把 State 里的解析结果切成条款，写回 ``clauses``。

    * 读 State：``parse_result``
    * 写 State：``clauses``
    """
    parse_result = state.get("parse_result")

    if parse_result is None or parse_result.status == "FAILED":
        # 没有可用的文档就切不出条款。这是可预期的结果，不是错误 ——
        # 写一个空列表而不是抛异常，图才能正常收尾。
        logger.info(
            "identify_clauses 跳过 | file_id=%s reason=%s",
            state.get("file_id"),
            "no_parse_result" if parse_result is None else "parse_failed",
        )
        return {"clauses": []}

    clauses = build_clauses(parse_result)

    logger.info(
        "identify_clauses 完成 | file_id=%s clauses=%d types=%s",
        state.get("file_id"),
        len(clauses),
        [c.clause_type for c in clauses],
    )
    return {"clauses": clauses}


__all__ = ["identify_clauses"]
