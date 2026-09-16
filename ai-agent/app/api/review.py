"""Agent 编排入口：``POST /api/agent/review``（P14-4 起为**异步启动**）。

它做什么
-------
``202 Accepted`` + ``{"task_id": N}``，然后**在后台**跑完整张 LangGraph。

::

    POST /api/agent/review
      │
      ├─ ① 取规则集快照            （请求内；失败 → 原有 422 语义）
      ├─ ② 文件落到本地临时文件
      ├─ ③ **预上传**到 Backend     （拿到真实的 review_task_id）
      └─ ④ 登记后台任务 → 202 {task_id}
                                   │
                                   ▼   （请求已结束）
                            后台跑完整张图
                                   │
                    ┌──────────────┴──────────────┐
                跑完且产出可用文档            抛异常 / 跑完但没产出
                    │                              │
              persist_risks → REVIEWED      调 Backend 标 BLOCKED

为什么改成异步（P14-4）
--------------------
真实 DeepSeek 调用实测单次 7~9 秒、整条链路同量级（P14-2）。这个量级下浏览器
请求虽然还撑得住，但**"HTTP 请求生命周期"与"图执行生命周期"绑死**本身就是个
隐患：LLM 长尾、大合同、并发都会把风险直接暴露成前端超时，而超时后 Agent
仍在跑（幽灵任务）。P14-4 把两者解耦，**状态的事实来源始终是 Backend 的
``ReviewTask``** —— Agent 不建自己的任务表。

为什么要有"预上传"这一步
----------------------
``task_id`` 由图内的 ``upload_file`` 节点创建，而 202 必须在图开始**之前**返回 ——
那一刻库里什么都还没有。因此请求阶段先自己调一次 Backend 的接入接口把任务建出来。

⚠️ 这不是"多建一个任务"：``contract_file.sha256`` 是全局 UNIQUE，第二次上传
（图内的 ``upload_file``）会命中 P4 冻结的幂等语义，拿到
``reused=true`` / ``task_reused=true`` 与**同一个** ``task_id``。
（预上传与图内上传拿到同一个 id，已由 P14-4 的集成测试钉住。）

它**不做**什么
------------
不校验文件类型/大小、不算 SHA-256、不判断幂等、不碰数据库 ——
这些都是 Backend 的职责，由 ``upload_file`` 节点通过 Backend 领域 API 完成。

为什么规则集仍在**请求内**取
--------------------------
规则取不到就不该"照常跑一遍"：那会白白建一个任务，而且真实失败原因会被
``rule_review`` 覆盖成"State 里没有 rule_snapshot"。留在请求内，失败可以
**立即**用原有的 422 语义如实返回（P14-4 裁决）。

更细的状态码映射（例如把 ``BACKEND_UNREACHABLE`` 映射成 502）属于后续的
ErrorClassifier，当前刻意不做。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status

from app.background import BackgroundReviews
from app.core.errors import AgentErrorCode
from app.graph.context import ReviewContext
from app.graph.state import ContractReviewState, has_usable_document
from app.llm.provider import LLMProvider
from app.rules.catalog import RuleSnapshotError, snapshot_from_backend
from app.rules.schemas import RuleSetSnapshot
from app.schemas.review import ReviewAcceptedResponse, ReviewRunResponse
from app.tools.backend_client import BackendClient, BackendRequestError

logger = logging.getLogger(__name__)

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


def _rejected_before_start(response: Response, error_code: str, message: str) -> ReviewRunResponse:
    """**启动阶段**就失败时的响应 —— 图没有启动，任务可能根本还没建出来。

    覆盖三种情况：规则集取不到 / 转换不了、预上传被拒、预上传响应缺少 ``task_id``。

    这条路径**不伪造一个空快照**、也不假装"已经受理"（那会让"我们没拿到规则"
    变成一次看起来正常的审查），更不抛给调用方一个 500 —— 它是"这次审查做不了"
    这个**可预期的结果**，与 P14-4 之前一样用 ``rejected`` + ``error_code`` 表达。

    ⚠️ 与"后台执行失败"的语义**不同**：那种请求已经 202 了，只能把任务标成
    ``blocked`` 让人去看；这种压根没开始，调用方当场就能拿到原因。
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


# --------------------------------------------------------------------------- #
# 后台执行（P14-4）
# --------------------------------------------------------------------------- #
#: 图**跑完了但没产出可用文档**时，把它的 ``error_code`` 翻成 Backend 的 ``BlockReasonCode``。
#:
#: ⚠️ 这里**抄字面量而不是 import**：取值必须与 Backend 的
#: ``app.core.constants.BlockReasonCode`` 一致，但 Agent **不能** import Backend 的代码
#: —— 两个服务各自独立部署，跨服务的契约只能靠值对齐，并由两侧的测试钉住
#: （Backend 侧有测试断言收到的码是登记过的枚举值）。
#:
#: 只映射能对上 §6.1 语义的两个：「加密/损坏 → FILE_CORRUPTED」「格式不支持 →
#: UNSUPPORTED_FORMAT」是文档里点名写过的。其余一律 ``INTERNAL_ERROR``
#: （字面意思就是"其它内部错误"），**不为此再造一批码**。
_BLOCK_REASON_INTERNAL_ERROR = "INTERNAL_ERROR"

_BLOCK_REASON_BY_AGENT_ERROR: dict[str, str] = {
    AgentErrorCode.PARSE_UNSUPPORTED_TYPE.value: "UNSUPPORTED_FORMAT",
    AgentErrorCode.PARSE_FAILED.value: "FILE_CORRUPTED",
}

#: 后台任务**抛异常**（图之外炸了）时用的阻塞原因。
#: 与上面那条路分开：图正常跑完只是结论不可用，和"后台执行本身崩了"是两件事，
#: 排查时看的地方也不同（Backend 侧 vs Agent 日志）。
_BLOCK_REASON_GRAPH_FAILED = AgentErrorCode.GRAPH_EXECUTION_FAILED.value

#: 含义为「**这件事此前已经做完了**」的拒绝码 —— 它们是**正常结果，不是失败**。
#:
#: ⚠️ 这一类**绝不能**把任务标成阻塞：同一个文件被重复提交（sha256 幂等会把它
#: 引到**同一个** task）时，图会在持久化阶段被 Backend 的 409 挡下。那说明
#: 这次审查**早就成功过**了 —— 若照常阻塞，一个已经 ``REVIEWED``、带着完整风险
#: 清单的任务会被莫名标成 ``blocked``，人工看到的就是"一次成功的审查失败了"。
#:
#: 取值来自 Backend 的 ``ErrorCode``（跨服务只能靠值对齐，见上面那段说明）。
_ALREADY_DONE_CODES: frozenset[str] = frozenset(
    {
        "DOCUMENT_ALREADY_PERSISTED",  # 文档层已写过（阶段已是 CLAUSED/REVIEWED）
        "TASK_ALREADY_PERSISTED",  # 风险层已写过
    }
)


async def _report_blocked(
    backend: BackendClient,
    task_id: int,
    *,
    reason_code: str,
    reason_msg: str,
) -> None:
    """如实把任务标成阻塞。**本函数自己绝不抛异常。**

    它只在失败路径上被调用 —— 调用点已经在处理另一个异常了，这里再抛会把它盖掉，
    于是"为什么失败"变成"上报失败"，真正的原因反而丢了。

    上报**失败**时只记日志：那时能做的已经做完了（Backend 连不上 / 任务已阻塞 /
    已完成），而任务的中间态在库里仍然可见（阶段停在原地），不至于静默消失。
    """
    outcome = await backend.block_review_task(
        task_id, reason_code=reason_code, reason_msg=reason_msg
    )
    if outcome.ok:
        logger.warning(
            "后台审查失败已如实上报 | task_id=%s reason_code=%s", task_id, reason_code
        )
        return

    logger.error(
        "后台审查失败，但上报也失败了 | task_id=%s reason_code=%s status=%s error_code=%s %s",
        task_id,
        reason_code,
        outcome.status_code,
        outcome.error_code,
        outcome.error_message,
    )


async def _execute_review(
    *,
    graph: Any,
    backend: BackendClient,
    llm: LLMProvider,
    task_id: int,
    temp_path: Path,
    filename: str,
    content_type: str | None,
    contract_no: str,
    title: str,
    contract_type: str,
    rule_snapshot: RuleSetSnapshot,
) -> None:
    """后台执行：跑完整张图，并**在任何结局下都留下痕迹**。

    三种结局各有各的处理，**没有一种是静默的**：

    ========================  ==================================================
    跑完且产出可用文档          ``persist_risks`` 已把 ``current_stage`` 推到
                              ``REVIEWED`` —— 成功路径**不在这里再写任何状态**
    跑完但没产出可用文档        调 Backend 标 ``blocked``。§6.1 早就规定了
    例如解析失败、被门禁拦下     「解析不可恢复错误 → blocked」，只是一直没有写入口
    **抛异常**                 记 ``exception`` 日志 + 标 ``blocked``
                              （``AGENT_GRAPH_EXECUTION_FAILED``）
    ========================  ==================================================

    ⚠️ 临时文件的生命周期**跟着本协程**走，不再跟着 HTTP 请求 —— 请求早就返回了。
    无论走哪条路都在 ``finally`` 里删掉。
    """
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
            context=ReviewContext(backend=backend, llm=llm),
        )
    except asyncio.CancelledError:
        # 服务关闭时被取消：审查**没有完成**，留下痕迹之后再让取消继续传播
        # （吞掉它的话 asyncio 会认为任务正常结束，注册表那边也看不出被取消）
        logger.warning("后台审查被取消（服务关闭？）| task_id=%s", task_id)
        await _report_blocked(
            backend,
            task_id,
            reason_code=_BLOCK_REASON_GRAPH_FAILED,
            reason_msg="后台审查被中断（服务关闭），未完成",
        )
        raise
    except Exception as exc:
        # ⚠️ 这里**不是**图内的失败（那种会写进 State 的 ``error_code``、不抛异常），
        # 而是"图之外炸了"。堆栈只进日志，**不进** block_reason_msg（那列会回到界面）
        logger.exception("后台审查执行失败 | task_id=%s", task_id)
        await _report_blocked(
            backend,
            task_id,
            reason_code=_BLOCK_REASON_GRAPH_FAILED,
            reason_msg=f"后台执行整张图时抛出 {type(exc).__name__}，详见 Agent 日志",
        )
        return
    finally:
        await asyncio.to_thread(temp_path.unlink, True)

    body = _to_response(final_state)
    if body.workflow_status == "completed":
        logger.info(
            "后台审查完成 | task_id=%s contract_id=%s file_id=%s",
            task_id,
            body.contract_id,
            body.file_id,
        )
        return

    if body.error_code in _ALREADY_DONE_CODES:
        # **正常结果，不是失败**：这个任务此前已经审完并落库，重复提交只会撞上幂等门禁。
        # 不阻塞、不改任何状态 —— 只记一条日志说明"为什么这次什么也没做"。
        logger.info(
            "后台审查被幂等门禁挡下（该任务此前已完成，无任何改动）| task_id=%s error_code=%s",
            task_id,
            body.error_code,
        )
        return

    # 图正常跑完，但这份输入产不出可审的内容 —— 不是 Agent 崩了，而是**这份合同没法审**。
    # §6.1 对此的规定就是 blocked，只是此前没有任何写入口（P14-4 才补上）。
    reason_code = _BLOCK_REASON_BY_AGENT_ERROR.get(
        body.error_code or "", _BLOCK_REASON_INTERNAL_ERROR
    )
    logger.warning(
        "后台审查未产出可用文档 | task_id=%s error_code=%s block_reason=%s",
        task_id,
        body.error_code,
        reason_code,
    )
    await _report_blocked(
        backend,
        task_id,
        reason_code=reason_code,
        reason_msg=body.error_message or "审查未能产出可用文档",
    )


@router.post(
    "/review",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
    summary="对一份合同文件启动 AI 审查编排（异步，返回 202 + task_id）",
    responses={
        202: {
            "model": ReviewAcceptedResponse,
            "description": "已受理：任务已建好、图已在后台启动。"
            "用 ``task_id`` 去 Backend 查进度与结果",
        },
        422: {
            "model": ReviewRunResponse,
            "description": "**启动阶段**就没能通过，图没有启动。``error_code`` 说明原因："
            "规则集取不到 / Backend 预上传被拒 / 预上传响应缺少 review_task_id",
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
) -> ReviewAcceptedResponse | ReviewRunResponse:
    """受理一次审查：**只做启动阶段的工作**，然后立刻返回 202。

    请求内完成三步：① 取规则集快照 ② 落临时文件 ③ 预上传拿 ``task_id``。
    任何一步失败都**不启动后台图**，并按原有语义返回 422（``rejected``）。

    请求返回后，完整图（``upload_file → … → persist_risks``）在后台执行：
    成功则由 ``persist_risks`` 把阶段推到 ``REVIEWED``；失败则把任务标成
    ``blocked`` 并写明原因。**Agent 不建自己的任务表** —— 状态的事实来源
    始终是 Backend 的 ``ReviewTask``。

    :raises: 不抛业务异常。启动阶段的失败是 ``rejected`` + ``error_code``（422）；
        后台的失败已经 202 了，只能如实写进 Backend 的任务状态。
    """
    backend: BackendClient = request.app.state.backend_client
    llm_provider: LLMProvider = request.app.state.llm_provider
    graph = request.app.state.review_graph
    background: BackgroundReviews = request.app.state.background_reviews

    # ---- ① 规则集：编排边界的职责，先拿到再启动 ----
    # 放在最前面：规则取不到就不该"照常跑一遍" —— 那会白白建一个任务，
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
        return _rejected_before_start(
            response, exc.error_code or AgentErrorCode.BACKEND_REJECTED.value, str(exc)
        )
    except RuleSnapshotError as exc:
        # 2xx 但内容不是一份可用快照：属于"响应缺少继续工作流所必需的字段"
        return _rejected_before_start(
            response, AgentErrorCode.BACKEND_CONTRACT_INCOMPLETE.value, str(exc)
        )

    # ---- ② 落临时文件 ----
    filename = file.filename or "unnamed"
    content_type = file.content_type
    temp_path = await _spool_to_temp(file, Path(filename).suffix.lower())
    await file.close()

    # ---- ③ 预上传：把任务建出来，202 才有 task_id 可给 ----
    # ``task_id`` 是图内的 ``upload_file`` 节点创建的，而 202 必须在图开始**之前**
    # 返回 —— 那一刻库里什么都还没有，所以这里先自己调一次领取接入接口。
    # 图的 ``upload_file`` 随后会因 ``sha256`` 幂等命中**同一个**任务（P4 冻结语义）。
    outcome = await backend.upload_contract(
        file_path=temp_path,
        filename=filename,
        contract_no=contract_no,
        title=title,
        contract_type=contract_type,
        content_type=content_type,
    )
    if not outcome.ok:
        await asyncio.to_thread(temp_path.unlink, True)
        return _rejected_before_start(
            response,
            outcome.error_code or AgentErrorCode.BACKEND_REJECTED.value,
            outcome.error_message or "预上传失败",
        )

    task_id = (outcome.payload or {}).get("review_task_id")
    if not isinstance(task_id, int):
        # 2xx 但缺了继续下去必需的字段 —— 与"规则集映不成快照"同一类问题
        await asyncio.to_thread(temp_path.unlink, True)
        return _rejected_before_start(
            response,
            AgentErrorCode.BACKEND_CONTRACT_INCOMPLETE.value,
            "Backend 的接入响应里没有 review_task_id，无法启动后台审查",
        )

    # ---- ④ 登记后台任务，立刻返回 ----
    # ⚠️ 临时文件交给后台协程删 —— 请求返回时图还没跑完
    background.start(
        _execute_review(
            graph=graph,
            backend=backend,
            llm=llm_provider,
            task_id=task_id,
            temp_path=temp_path,
            filename=filename,
            content_type=content_type,
            contract_no=contract_no,
            title=title,
            contract_type=contract_type,
            rule_snapshot=rule_snapshot,
        ),
        name=f"review:{task_id}",
    )

    response.status_code = status.HTTP_202_ACCEPTED
    return ReviewAcceptedResponse(task_id=task_id)


__all__ = ["UPLOAD_CHUNK_SIZE", "router", "run_review"]
