"""Agent 编排入口：``POST /api/agent/review``。

它做什么
-------
接收一份合同文件 + 业务元数据 → 跑完 LangGraph 工作流 → 返回结果。

它**不做**什么
------------
不校验文件类型/大小、不算 SHA-256、不判断幂等、不碰数据库 ——
这些都是 Backend 的职责，由 ``upload_file`` 节点通过 Backend 领域 API 完成。
本模块只负责**传输层与编排边界**：把上传流落到本机临时文件、
**取规则集快照**、组装初始 State、把结果投影成响应。

为什么规则集在**这里**取，而不是在 ``rule_review`` 节点里
-----------------------------------------------------
节点只消费"已经拿到的快照"（P8-2 第二小步定下的契约）。把 HTTP 塞进节点，
"规则审查"这个纯编排步骤就突然依赖网络，节点的失败语义与可测性都会变浑。
取规则属于**编排的前置输入准备** —— 与"组装初始 State"是同一件事。

为什么 Agent 要把文件落到本地临时文件
----------------------------------
Frontend 把文件交给 Agent，Agent 再转交给 Backend。中间这一跳必须有一份可读的
本地副本，``upload_file`` 节点才能把它作为 multipart 发给 Backend。
工作流结束后无论成败都在 ``finally`` 里删除 —— **这不是 Backend 那套孤儿清理的一部分**，
只是一个请求内的临时文件生命周期。

关于状态码
---------
``200`` ⇔ 工作流**成功产出且没有失败标记**；其余一律 ``422``，具体原因在 ``error_code`` 里
（取值见 ``AgentErrorCode``）。422 覆盖：被门禁拦下、解析失败、解析没跑过，
以及**后续节点判定失败**（如 ``rule_review`` 没拿到规则快照 —— 输入缺失）。

状态码由 ``workflow_status`` 直接推导，**不单独判断** —— 否则会出现
"上传成功但文档读不出来"被报成 200 的语义矛盾。

更细的状态码映射（例如把 ``BACKEND_UNREACHABLE`` 映射成 502）属于后续的
ErrorClassifier，当前刻意不做。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status

from app.core.errors import AgentErrorCode
from app.graph.context import ReviewContext
from app.graph.state import ContractReviewState, has_usable_document
from app.llm.provider import LLMProvider
from app.rules.catalog import RuleSnapshotError, snapshot_from_backend
from app.rules.schemas import RuleSetSnapshot
from app.schemas.review import ReviewRunResponse
from app.tools.backend_client import BackendClient, BackendRequestError

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


def _rule_set_failure(response: Response, error_code: str, message: str) -> ReviewRunResponse:
    """规则集取不到 / 转换不了时的响应。

    这条路径**不伪造一个空快照**（那会让"我们没拿到规则"变成一次看起来正常的审查），
    也不抛给调用方一个 500 —— 它是"这次审查做不了"这个**可预期的结果**，
    与其它失败一样用 ``rejected`` + ``error_code`` 表达。
    """
    response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return ReviewRunResponse(workflow_status="rejected", error_code=error_code, error_message=message)


def _to_response(state: ContractReviewState) -> ReviewRunResponse:
    """把 Graph 的最终状态投影成响应 DTO。

    Graph 的 State 是 Agent 内部的工作记忆，不直接对外暴露 ——
    它可能包含后续阶段才会增加的字段，且字段名会随内部实现变化。

    ``workflow_status`` 的判据
    ------------------------
    三个条件**同时**成立才算 ``completed``：

    1. ``error_code is None`` —— **State 里没有失败标记**
    2. ``file_valid is True`` —— 门禁通过
    3. 解析产出了可用文档（``has_usable_document``）

    只看门禁与解析，会让"上传成功但文档读不出来"被报成 ``completed`` ——
    明明没产出任何可用内容，却说工作流完成了。
    而只看前两条，会让**后续节点**（如 ``rule_review``）判定失败时，
    响应依旧是 ``completed + error_code`` 这种自相矛盾的组合。

    ⚠️ 这一层只做 **projection**：失败是 State 已经判定好的事实
    （哪个节点、为什么失败都在 ``error_code`` / ``error_message`` 里），
    API 不去重新判断业务（例如"有没有规则快照"是 ``rule_review`` 的事）。
    """
    succeeded = (
        state.get("error_code") is None and state.get("file_valid") is True and has_usable_document(state)
    )
    return ReviewRunResponse(
        workflow_status="completed" if succeeded else "rejected",
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
        200: {"description": "工作流成功产出且没有失败标记（workflow_status = completed）"},
        422: {
            "description": "本次审查未能完成，具体原因见 error_code："
            "被门禁拦下 / 解析失败 / 解析没跑过 / 规则集取不到 / 规则审查缺少输入"
        },
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
    """跑一次完整编排：取规则集 → ``upload_file → validate_file → parse_document →
    identify_clauses → extract_metadata → extract_keywords → rule_review``。

    文件与业务元数据原样转发给 Backend（由 ``upload_file`` 节点完成），
    Agent 不在这一跳上做任何业务判断；规则集由本层取好后随初始 State 进图。

    :raises: 不抛业务异常 —— 规则集取不到、门禁不过、解析失败都是**可预期的结果**，
        一律表达成 ``workflow_status="rejected"`` + ``error_code``（HTTP 422）。
    """
    backend: BackendClient = request.app.state.backend_client
    llm_provider: LLMProvider = request.app.state.llm_provider
    graph = request.app.state.review_graph

    # ---- 规则集：编排边界的职责，先拿到再跑图 ----
    # 放在最前面：规则取不到就不该"照常跑一遍" —— 那会白白上传一次，
    # 而且真实失败原因会被 ``rule_review`` 覆盖成"State 里没有 rule_snapshot"。
    #
    # ⚠️ 空快照的**唯一来源**是 Backend 明确回答"该合同类型没有启用的规则集"
    # （200 + ``rule_set=null``）。取不到、或取回来的东西映不成快照，
    # 都**不得**退化成空快照 —— 那是把"没拿到规则"伪装成"这份合同没有风险"。
    try:
        rule_snapshot: RuleSetSnapshot = snapshot_from_backend(
            await backend.get_effective_rule_set(contract_type)
        )
    except BackendRequestError as exc:
        # 连不上 / 非 2xx：保留 Backend 给的错误码，没有就用 BACKEND_REJECTED
        return _rule_set_failure(response, exc.error_code or AgentErrorCode.BACKEND_REJECTED.value, str(exc))
    except RuleSnapshotError as exc:
        # 2xx 但内容不是一份可用快照：属于"响应缺少继续工作流所必需的字段"
        return _rule_set_failure(response, AgentErrorCode.BACKEND_CONTRACT_INCOMPLETE.value, str(exc))

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
                # 规则集快照随初始 State 进入图 —— ``rule_review`` 只消费它，不自己去取
                "rule_snapshot": rule_snapshot,
            },
            context=ReviewContext(backend=backend, llm=llm_provider),
        )
    finally:
        await asyncio.to_thread(temp_path.unlink, True)

    body = _to_response(final_state)

    if body.workflow_status != "completed":
        # 工作流没能产出可用文档：不是服务出错，而是这份输入的结果不可用
        # （被门禁拦下 / 解析失败 / 解析没跑过）。
        # 状态码从**响应体**推导，保证两者永远一致 —— 不再单独判断 file_valid，
        # 否则"上传成功但文档读不出来"会变成 200 + rejected 这种自相矛盾的返回。
        response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT

    return body


__all__ = ["UPLOAD_CHUNK_SIZE", "router", "run_review"]
