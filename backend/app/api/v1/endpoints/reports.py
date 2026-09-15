"""审查报告导出接口（P12-3）。

本模块**只做传输层**：解析路径参数、调 Service、把 Markdown 装进响应。
它不做查询、不做渲染、不决定报告内容 —— 那些在
``app/services/report_query.py`` 与 ``app/services/report_render.py``。

与 P11 工作台的关系
------------------
两者都是**只读投影**，读的是同一批已持久化的审查结果，但服务的目的不同：

* ``GET /review-tasks/{id}/workbench`` —— 给前端渲染交互界面（带高亮坐标）
* ``GET /review-tasks/{id}/report/export`` —— 给**人**一份可以带走的文档

因此报告**不查 ``document_block``**（不需要原文坐标），也**不重新计算**任何东西
（不重跑规则、不调 LLM、不合并风险、不产生审查结论）。它是当前库内容的一份
确定性快照。

为什么不落一份报告文件
--------------------
不建 ``report`` 表、不写磁盘：报告是**纯只读投影**，每个字都能从既有表确定性
重现。存一份快照就是制造第二个真相源，两者漂移时无人负责。重复下载因此是
天然幂等的 —— 没有状态，也就没有"报告过期 / 需重新生成"。
"""

from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Path, Response

from app.core.logging import get_logger
from app.services.report_query import load_report_data
from app.services.report_render import ReportData, render_markdown

logger = get_logger(__name__)

router = APIRouter(tags=["reports"])

#: Markdown 的 media type。带 charset：报告是中文文本，不声明编码会让部分客户端猜 GBK。
REPORT_MEDIA_TYPE = "text/markdown; charset=utf-8"

#: 文件名里不允许出现的字符（路径分隔符 + Windows 保留字符 + 控制字符）。
#: ``contract_no`` 来自上传表单，不是可信输入 —— 一个 ``/`` 就能让文件名变成路径。
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


@router.get(
    "/review-tasks/{task_id}/report/export",
    response_class=Response,
    summary="导出一次审查的 Markdown 报告",
    responses={
        200: {
            "description": "导出成功，响应体是最终的 Markdown 报告全文",
            "content": {REPORT_MEDIA_TYPE: {}},
        },
        404: {
            "description": "任务不存在（``TASK_NOT_FOUND``），"
            "或任务关联的合同 / 附件缺失（``CONTRACT_NOT_FOUND`` / ``FILE_NOT_FOUND``）"
        },
        409: {
            "description": "任务尚未完成风险审查（``REPORT_NOT_READY``）——"
            "``current_stage`` 不是 ``REVIEWED``。此时风险还没落库，"
            "导出会得到一份**零风险**的报告，把它当成「没有风险」是有害的"
        },
    },
)
async def export_report(
    task_id: Annotated[int, Path(ge=1, description="审查任务 ID")],
) -> Response:
    """把一次审查的结果导出成 Markdown 文件。

    **没有副作用**：不写库、不建报告记录、不需要 ``report_id``。因此重复下载
    永远允许，也永远得到同样的字节（渲染是确定性的，连"生成时间"都没有 ——
    见 ``report_render.render_markdown``）。

    阶段门禁在 ``load_report_data`` 里：只有 ``REVIEWED`` 的任务能出报告，
    否则 409 ``REPORT_NOT_READY``。
    """
    data = await load_report_data(task_id)
    markdown = render_markdown(data)
    filename = report_filename(data)

    logger.info(
        "导出审查报告 | task_id=%s risks=%d bytes=%d",
        task_id,
        len(data.risks),
        len(markdown.encode("utf-8")),
    )

    return Response(
        content=markdown,
        media_type=REPORT_MEDIA_TYPE,
        headers={"Content-Disposition": content_disposition(filename, data)},
    )


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #
def report_filename(data: ReportData) -> str:
    """下载时的文件名（**含中文**）：``HT-2026-001-审查报告-42.md``。

    ``contract_no`` 为空时退回 ``report``；任务号进文件名是因为同一个合同
    可以有多条审查任务，不带任务号的两个文件会互相覆盖。
    """
    contract_no = _ILLEGAL_FILENAME_CHARS.sub("-", data.contract.contract_no).strip(" .-")
    return f"{contract_no or 'report'}-审查报告-{data.task.task_id}.md"


def content_disposition(filename: str, data: ReportData) -> str:
    """RFC 6266 / RFC 5987 双写法：``filename`` 走 ASCII 兜底，``filename*`` 带中文。

    ::

        attachment; filename="HT-2026-001-review-report-42.md";
                    filename*=UTF-8''HT-2026-001-%E5%AE%A1%E6%9F%A5%E6%8A%A5%E5%91%8A-42.md

    为什么要两份：

    * ``filename*=UTF-8''…`` 是**现代客户端**读的那个，中文名靠它
    * HTTP 头的值**只能是 latin-1**。把中文直接塞进 ``filename="…"`` 会在
      编码环节炸掉或产出乱码 —— 这是"中文文件名"最常见的翻车点
    * 保留纯 ASCII 的 ``filename`` 是给极老的客户端兜底：它们不认识 ``filename*``，
      但至少能存下一个文件名正确的文件，而不是一个乱码名
    """
    return (
        f'attachment; filename="{_ascii_fallback(data)}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _ascii_fallback(data: ReportData) -> str:
    """ASCII 兜底名：把非 ASCII 字符全部替换成 ``-``。"""
    contract_no = re.sub(r"[^A-Za-z0-9._-]", "-", data.contract.contract_no).strip(" .-")
    return f"{contract_no or 'contract'}-review-report-{data.task.task_id}.md"


__all__ = ["REPORT_MEDIA_TYPE", "content_disposition", "export_report", "report_filename", "router"]
