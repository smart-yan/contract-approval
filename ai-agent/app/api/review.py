"""Agent 编排入口：``POST /api/agent/review``。

它做什么
-------
接收一份合同文件 + 业务元数据 → 跑完 LangGraph 工作流 → 返回结果。

它**不做**什么
------------
不校验文件类型/大小、不算 SHA-256、不判断幂等、不碰数据库 ——
这些都是 Backend 的职责，由 ``upload_file`` 节点通过 Backend 领域 API 完成。
本模块只负责**传输层**：把上传流落到本机临时文件、组装初始 State、把结果投影成响应。

为什么 Agent 要把文件落到本地临时文件
----------------------------------
Frontend 把文件交给 Agent，Agent 再转交给 Backend。中间这一跳必须有一份可读的
本地副本，``upload_file`` 节点才能把它作为 multipart 发给 Backend。
工作流结束后无论成败都在 ``finally`` 里删除 —— **这不是 Backend 那套孤儿清理的一部分**，
只是一个请求内的临时文件生命周期。

关于 422
-------
工作流被 Gate 拦下时返回 422，具体原因在 ``error_code`` 里（取值见 ``AgentErrorCode``）。
更细的状态码映射（例如把 ``BACKEND_UNREACHABLE`` 映射成 502）属于 P6 的 ErrorClassifier，
P5-4 刻意不做。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status

from app.graph.context import ReviewContext
from app.graph.state import ContractReviewState
from app.schemas.review import ReviewRunResponse
from app.tools.backend_client import BackendClient

router = APIRouter(prefix="/api/agent", tags=["review"])

#: 落盘时的读取块大小（1 MiB）。与 Backend 一致：绝不把整份文件读进内存。
UPLOAD_CHUNK_SIZE = 1024 * 1024


async def _spool_to_temp(upload: UploadFile, suffix: str) -> Path:
    """把上传流分块写入临时文件，返回其路径。

    失败时自行清理已创建的临时文件，不把垃圾留给调用方。
    """
    fd, name = tempfile.mkstemp(prefix="agent-upload-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as fp:
            while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                # 本地文件写入是同步阻塞 IO，放到线程池（与 Backend 同样的理由）
                await asyncio.to_thread(fp.write, chunk)
    except Exception:
        await asyncio.to_thread(path.unlink, True)
        raise
    return path


def _to_response(state: ContractReviewState) -> ReviewRunResponse:
    """把 Graph 的最终状态投影成响应 DTO。

    Graph 的 State 是 Agent 内部的工作记忆，不直接对外暴露 ——
    它可能包含后续阶段才会增加的字段，且字段名会随内部实现变化。
    """
    completed = state.get("file_valid") is True
    return ReviewRunResponse(
        workflow_status="completed" if completed else "rejected",
        contract_id=state.get("contract_id"),
        file_id=state.get("file_id"),
        review_task_id=state.get("review_task_id"),
        sha256=state.get("sha256"),
        file_type=state.get("file_type"),
        reused=state.get("reused"),
        task_reused=state.get("task_reused"),
        validation_errors=list(state.get("validation_errors") or []),
        parse_result=state.get("parse_result"),
        error_code=state.get("error_code"),
        error_message=state.get("error_message"),
    )


@router.post(
    "/review",
    response_model=ReviewRunResponse,
    summary="对一份合同文件执行 AI 审查编排",
    responses={
        200: {"description": "工作流走完全程"},
        422: {"description": "工作流在该文件上被拦下，具体原因见 error_code"},
    },
)
async def run_review(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File(description="合同文件：DOCX / PDF / JPEG / PNG")],
    contract_no: Annotated[str, Form(min_length=1, max_length=64, description="合同编号")],
    title: Annotated[str, Form(min_length=1, max_length=255, description="合同名称")],
    contract_type: Annotated[str, Form(min_length=1, max_length=32, description="合同类型")],
) -> ReviewRunResponse:
    """跑一次 ``upload_file → validate_file → parse_document`` 编排。

    文件与业务元数据原样转发给 Backend（由 ``upload_file`` 节点完成），
    Agent 不在这一跳上做任何业务判断。
    """
    backend: BackendClient = request.app.state.backend_client
    graph = request.app.state.review_graph

    filename = file.filename or "unnamed"
    content_type = file.content_type
    temp_path = await _spool_to_temp(file, Path(filename).suffix.lower())
    await file.close()

    try:
        final_state: ContractReviewState = await graph.ainvoke(
            {
                "file_path": str(temp_path),
                "filename": filename,
                "content_type": content_type,
                "contract_no": contract_no,
                "title": title,
                "contract_type": contract_type,
            },
            context=ReviewContext(backend=backend),
        )
    finally:
        await asyncio.to_thread(temp_path.unlink, True)

    if final_state.get("file_valid") is not True:
        # 工作流被拦下：不是服务出错，而是这份输入没能通过 Gate。
        # 具体原因在响应体的 error_code / validation_errors 里。
        response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT

    return _to_response(final_state)


__all__ = ["UPLOAD_CHUNK_SIZE", "router", "run_review"]
