"""文档层写入接口（P10-1）。

本模块**只做传输层**：解析路径参数与请求体、调 Service、返回响应。
不解析文档、不算坐标、不切条款、不抽元数据 —— 那些是 Agent 的职责；
也不碰事务与行锁 —— 那些在 ``app/services/document_persistence.py`` 里，
整体处在**一个**事务内。

路径为什么挂在 ``review-tasks/{task_id}`` 下面
-------------------------------------------
与风险写入接口（P9-10）同一理由：这批文档结果是**某一次审查**的产出
（``clause.task_id`` / ``contract_metadata.task_id`` 都是 NOT NULL），
挂在任务下，路由本身就表达了归属，也避免请求体里再写一遍 ``task_id``
而制造"路径说 A、请求体说 B"的不一致。

⚠️ 与风险接口的关系
------------------
两个接口**不合并**，它们对应流水线上先后两段：本接口是"审查的输入"
（解析 / 条款 / 元数据），风险接口是"审查的输出"。数据依赖也决定了顺序 ——
``clause`` 引用 ``document_block``、``risk_item`` 引用 ``clause``，
因此必须先文档层、后风险层。风险接口（P9-10）**一行不改**。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, status

from app.core.logging import get_logger
from app.schemas.document import DocumentPersistRequest, DocumentPersistResponse
from app.services.document_persistence import persist_task_document

logger = get_logger(__name__)

router = APIRouter(tags=["documents"])


@router.post(
    "/review-tasks/{task_id}/document",
    response_model=DocumentPersistResponse,
    status_code=status.HTTP_201_CREATED,
    summary="整批持久化一次审查的文档层结果（块 / 条款 / 元数据）",
    responses={
        201: {"description": "整批写入成功，附件解析状态与任务阶段已更新"},
        404: {"description": "任务不存在（``TASK_NOT_FOUND``）或附件不存在（``FILE_NOT_FOUND``）"},
        409: {
            "description": "该任务已写入过文档结果（``DOCUMENT_ALREADY_PERSISTED``）；"
            "复用块时结构与本次不一致（``DOCUMENT_BLOCKS_CONFLICT``）；"
            "附件解析状态已是终态且与本次不一致（``PARSE_STATUS_ALREADY_FINAL``）"
        },
        422: {"description": "请求体校验失败（含块引用越界、``parse_status`` 非法）"},
    },
)
async def persist_document(
    task_id: Annotated[int, Path(ge=1, description="审查任务 ID")],
    payload: DocumentPersistRequest,
) -> DocumentPersistResponse:
    """把一批文档层结果**原子地**写入该任务。

    请求体里的 ``start_block_index`` / ``end_block_index`` / ``source_block_index``
    都是 **``blocks[]`` 数组的下标**，不是数据库主键 —— 主键在本请求处理过程中才产生
    （见 ``app/schemas/document.py`` 的说明）。

    任何一条校验不过 → **整批不写**；块、条款、元数据与解析状态更新同属一个事务。
    """
    return await persist_task_document(task_id, payload)


__all__ = ["persist_document", "router"]
