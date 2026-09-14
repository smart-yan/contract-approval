"""合同接入接口（架构文档 §8：``POST /contracts``）。

本模块**只做传输层的事**：解析表单、把上传流落到临时文件、调用 Service、组装响应。
业务编排（三段短事务、去重、并发处理、补偿清理）全部在
``app.services.contract_ingest`` 里，这里不做任何业务判断。

为什么在这里流式落盘而不是先读进内存
------------------------------------
``UploadFile.read()`` 无参调用会把整个文件读进内存。50MB × 并发上传足以打爆内存。
因此按块读取、边读边写、边累加大小，一超限立刻中断。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from app.core.constants import ContractType
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.schemas.contract import ContractIngestResponse
from app.services.contract_ingest import ContractMeta, ingest_contract
from app.storage import get_storage
from app.utils.file_utils import MAX_UPLOAD_SIZE_BYTES, normalize_extension

logger = get_logger(__name__)

router = APIRouter(tags=["contracts"])

#: 落盘时的读取块大小（1 MiB）
UPLOAD_CHUNK_SIZE = 1024 * 1024


async def _stream_upload_to_temp(upload: UploadFile, temp_path: Path) -> int:
    """把上传流按块写入临时文件，返回实际字节数。

    **边写边判大小**：不能等写完再检查 —— 那样磁盘和内存都已经付出代价了。
    返回的是自己数出来的字节数，而不是 Content-Length（后者可被伪造）。
    """
    size = 0
    fp = await asyncio.to_thread(temp_path.open, "wb")
    try:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_SIZE_BYTES:
                raise AppError(
                    code=ErrorCode.FILE_TOO_LARGE,
                    message=f"文件超过大小上限 {MAX_UPLOAD_SIZE_BYTES} 字节",
                    details={"max_size": MAX_UPLOAD_SIZE_BYTES},
                )
            # 本地文件写入属同步阻塞 IO，按 §1.3 放到线程池
            await asyncio.to_thread(fp.write, chunk)
    finally:
        await asyncio.to_thread(fp.close)
    return size


@router.post(
    "/contracts",
    response_model=ContractIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传合同文件并创建审查任务",
    responses={
        200: {"description": "该 sha256 已存在，复用了已有合同/附件/任务"},
        413: {"description": "文件超过大小上限"},
        415: {"description": "不支持的文件类型"},
        409: {"description": "合同编号已存在"},
    },
)
async def upload_contract(
    response: Response,
    file: Annotated[UploadFile, File(description="合同文件：DOCX / PDF / JPEG / PNG / TIFF / BMP")],
    contract_no: Annotated[
        str, Form(min_length=1, max_length=64, description="合同编号（业务事实，不自动生成）")
    ],
    title: Annotated[str, Form(min_length=1, max_length=255, description="合同名称")],
    contract_type: Annotated[ContractType, Form(description="合同类型，决定后续使用哪套规则集")],
    our_party: Annotated[str | None, Form(max_length=255)] = None,
    counterparty: Annotated[str | None, Form(max_length=255)] = None,
    amount: Annotated[Decimal | None, Form()] = None,
    currency: Annotated[str | None, Form(max_length=3)] = None,
    sign_date: Annotated[date | None, Form()] = None,
    effective_date: Annotated[date | None, Form()] = None,
    expire_date: Annotated[date | None, Form()] = None,
    dept: Annotated[str | None, Form(max_length=64)] = None,
) -> ContractIngestResponse:
    """接收合同文件，完成校验 → 去重 → 落盘 → 建合同/附件/任务。

    **同一个文件重复上传不会新建任何记录**：命中 sha256 时直接复用已有
    合同、附件与审查任务，并返回 ``reused=true``（HTTP 200）。
    """
    storage = get_storage()
    temp_path = storage.new_temp_path(suffix=normalize_extension(file.filename))

    try:
        size = await _stream_upload_to_temp(file, temp_path)
    except Exception:
        # 流转失败（含超限中断）：临时文件由本层负责清理，
        # 因为此时还没进入 Service，Service 的 finally 不会执行
        await asyncio.to_thread(temp_path.unlink, True)
        raise
    finally:
        await file.close()

    meta = ContractMeta(
        contract_no=contract_no,
        title=title,
        contract_type=contract_type.value,
        our_party=our_party,
        counterparty=counterparty,
        amount=amount,
        currency=currency,
        sign_date=sign_date,
        effective_date=effective_date,
        expire_date=expire_date,
        dept=dept,
    )

    # Service 负责在结束时清理 temp_path
    result = await ingest_contract(
        temp_path=temp_path,
        filename=file.filename or "unnamed",
        content_type=file.content_type,
        size=size,
        meta=meta,
    )

    if result.reused and result.task_reused:
        # 文件与任务都是复用的 ⇒ 本次没有创建任何资源，200 比 201 准确。
        # 只满足其中一个时说明**确实创建了新东西**（新合同/附件，或新审查任务），仍是 201。
        response.status_code = status.HTTP_200_OK

    return ContractIngestResponse(**asdict(result))
