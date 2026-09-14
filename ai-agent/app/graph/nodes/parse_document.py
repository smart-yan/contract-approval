"""``parse_document`` 节点 —— **P5-3 桩实现**。

范围声明（架构裁决）
------------------
P5-3 的目标是"证明 Graph 骨架能跑"，因此本节点刻意：

* **不**安装 / 引入 PyMuPDF、python-docx 等文档解析依赖
* **不**真实读取 DOCX / PDF / 图片内容
* **不**伪造合同正文（``ParseResult.text`` 恒为空串）

它只做一件事：记录"流程已进入解析阶段"，并产出后续阶段要填充的 ``ParseResult``
形状，以此证明**节点编排成立**（State 传进来了、节点执行了、结果写回 State 了）。

Parser 的最终归属
----------------
真实 Parser（``DocumentParser`` 抽象 / ``DocxParser`` / ``PdfParser`` / ``ImageParser``）
属于 ``ai-agent/app/parsers/``，**P5-3 不创建该包**；Backend 的 ``app/parsers/``
保持为空目录。

刻意保持同步函数
--------------
没有 IO 就不该是 async。真实解析器接入后这里会改成 async，并把阻塞的解析工作
交给线程池（架构文档 §1.3），但那一步属于 P7。
"""

from __future__ import annotations

import logging

from app.graph.state import ContractReviewState
from app.schemas.document import ParseResult

logger = logging.getLogger(__name__)

#: 桩解析器标识。真实 Parser 属于 P7。
STUB_PARSER_NAME = "StubParser"


def parse_document(state: ContractReviewState) -> dict[str, object]:
    """记录已进入解析阶段，产出最小 ``ParseResult``。

    * 读 State：``file_id`` / ``file_type``
    * 写 State：``parse_result``
    """
    result = ParseResult(
        parser=STUB_PARSER_NAME,
        source_file_type=state.get("file_type") or "UNKNOWN",
    )
    logger.info(
        "parse_document 完成（桩，不产出正文） | file_id=%s type=%s",
        state.get("file_id"),
        result.source_file_type,
    )
    return {"parse_result": result}


__all__ = ["STUB_PARSER_NAME", "parse_document"]
