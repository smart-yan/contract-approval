"""Agent 的「文档层持久化」业务动作（P10-3）。

它是**边界层**：一边是 Agent 的解析/理解产物，另一边是 Backend 的持久化契约，
字段映射与**坐标换算**只在这里发生一次。

::

    ParseResult.paragraphs   ─┐
    Clause[]                 ─┼─ 本模块的映射函数（唯一的映射点）
    MetadataItem[]           ─┘
              ▼
    DocumentPersistRequest（Backend 的请求契约）
              │  BackendClient.persist_task_document
              ▼
    document_block / clause / contract_metadata

三处必须在边界上完成的事
----------------------
**1. 全局字符偏移**（Backend 的 ``document_block`` 要求 NOT NULL）

坐标系由 P6-2 的契约唯一确定：``ParseResult.text`` 恒等于各段落 ``text`` 以 ``\\n``
拼接，且**段落文本内不含 ``\\n``**。因此第 i 段的区间是**可确定性推导**的：

::

    char_start_global[i] = Σ(len(text[j]) + 1) for j < i
    char_end_global[i]   = char_start_global[i] + len(text[i])

这一段计算刻意放在 Agent 侧：文本与坐标契约都归它，Backend 只存不算
（P10-1 的 DTO 说明里写明了「由 Agent 计算」）。

**2. 条款区间的坐标换算**

Agent 的 ``Clause`` 用**段落号**表达区间（``start_paragraph_index`` /
``end_paragraph_index``），而 Backend 的请求要用**``blocks[]`` 数组的下标**表达
（``start_block_index`` / ``end_block_index`` —— block 的数据库主键要等写入时才产生，
调用方无从得知）。两者在本项目里恰好相等（段落号从 0 连续递增，blocks 按同序排），
但映射**仍走显式换算**而不是直接赋值：万一将来段落号出现空洞或重排，
直接赋值会静默错位。

**3. ``parse_status`` 的三态收敛**

``PARSED`` / ``EMPTY`` → ``PARSED``；``FAILED`` → ``FAILED``。
``EMPTY``（解析成功但正文为空）**不是失败** —— 它是"文档本身没内容"这个数据问题，
Backend 侧用"零个块"表达，不需要新状态。

本地映射不成立时**抛异常**，不返回 ``ok=False``
--------------------------------------------
两类失败的来源不同，因此处理方式不同：

* **Backend 拒绝**（409 / 404 / 422）→ ``ok=False``，由节点写进 State。
  那是外部结果，要如实上报。
* **本地映射不成立**（条款引用了不存在的段落、区间倒置）→ **抛**
  :class:`DocumentMappingError`。这不是外部失败，而是**我们自己的理解层
  自相矛盾**：条款切分说"第 5 段到第 3 段"，或引用了 ``paragraphs`` 里根本没有的段落号。
  生成一个"越界的 DTO"发给 Backend 只会被它拒绝（P10-1 的 DTO 会校验收口），
  那样错误就变成了"Backend 拒绝"这种含糊说法 —— 问题出在我们这边，
  就该在我们这边响亮地失败。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.schemas.document import ParseResult
from app.schemas.understanding import Clause, MetadataItem
from app.tools.backend_client import BackendClient


class DocumentMappingError(Exception):
    """Agent 的解析/理解产物**自相矛盾**，无法映射成合法的 Backend 请求。

    与 :class:`~app.tools.backend_client.BackendRequestError` 的区别在于**责任方**：
    那个是"Backend 拒绝了我们的请求"，这个是"我们自己的产物根本拼不出一条请求"。
    混成一个异常会让排查时分不清该去看 Backend 日志还是看条款切分。
    """


@dataclass(frozen=True, slots=True)
class DocumentPersistenceRequest:
    """一次文档层持久化所需的输入。

    :param parse_result: ``parse_document`` 的产出（段落与全文的**唯一**来源）
    :param clauses: ``identify_clauses`` 的产出。解析失败时传空
    :param metadata: ``extract_metadata`` 的产出。解析失败时传空
    """

    task_id: int
    parse_result: ParseResult
    clauses: Sequence[Clause] = field(default_factory=tuple)
    metadata: Sequence[MetadataItem] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DocumentPersistenceResult:
    """文档层持久化的输出契约。

    ``ok=False`` 时 ``error_code`` / ``error_message`` 有值 —— ``error_code``
    可能是 Agent 侧的错误码（连不上 / 响应不是 JSON），也可能是 Backend 自己的
    错误码（``DOCUMENT_ALREADY_PERSISTED`` / ``DOCUMENT_BLOCKS_CONFLICT`` /
    ``PARSE_STATUS_ALREADY_FINAL``）**原样带回**。
    """

    ok: bool
    parse_status: str | None = None
    blocks_created: int | None = None
    blocks_reused: int | None = None
    clauses_persisted: int | None = None
    metadata_persisted: int | None = None
    current_stage: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class DocumentPersistenceTool:
    """Agent 的「把解析/理解结果写回 Backend」业务动作。

    :param backend: HTTP 通信层。Tool 只依赖它的抽象行为 ——
        测试里换一个挂 ``httpx.MockTransport`` 的实例就能跑。
    """

    def __init__(self, backend: BackendClient) -> None:
        self._backend = backend

    async def run(self, request: DocumentPersistenceRequest) -> DocumentPersistenceResult:
        """把段落 / 条款 / 元数据**一次**提交给 Backend。

        :raises DocumentMappingError: 本地产物自相矛盾（见模块 docstring）

        **失败时也会发请求**：``parse_status=FAILED`` 是一次真实结论，
        Backend 要据此记录"这个文件这次没解析出来"并把任务阶段推进。
        跳过它会让失败文件的 ``parse_status`` 永远停在 ``PENDING``，
        而且永远无法被后续任务升级（P10-1 的迁移规则要求从 ``PENDING`` 或 ``FAILED`` 出发）。
        """
        parse_status = _parse_status_of(request.parse_result)
        positions = _block_positions(request.parse_result)

        outcome = await self._backend.persist_task_document(
            request.task_id,
            parse_status=parse_status,
            blocks=_blocks_of(request.parse_result),
            clauses=[_clause_payload(clause, positions) for clause in request.clauses],
            metadata=[_metadata_payload(item, positions) for item in request.metadata],
        )

        if not outcome.ok or outcome.payload is None:
            return DocumentPersistenceResult(
                ok=False,
                error_code=outcome.error_code,
                error_message=outcome.error_message,
            )

        payload = outcome.payload
        return DocumentPersistenceResult(
            ok=True,
            parse_status=_as_str(payload.get("parse_status")),
            blocks_created=_as_int(payload.get("blocks_created")),
            blocks_reused=_as_int(payload.get("blocks_reused")),
            clauses_persisted=_as_int(payload.get("clauses_persisted")),
            metadata_persisted=_as_int(payload.get("metadata_persisted")),
            current_stage=_as_str(payload.get("current_stage")),
        )


# --------------------------------------------------------------------------- #
# 映射：Agent 产物 → Backend 请求条目
# --------------------------------------------------------------------------- #
def _parse_status_of(parse_result: ParseResult) -> str:
    """把 ParseResult 的三态收敛成 Backend 收的两个终态。

    ``EMPTY`` **不是失败**：解析成功了，只是文档本身没有正文（数据问题）。
    Backend 侧用"零个块"表达它，因此这里与 ``PARSED`` 同路 ——
    ``ParseStatus`` 里没有 EMPTY，也不该为了它新造一个。
    """
    return "FAILED" if parse_result.status == "FAILED" else "PARSED"


def _blocks_of(parse_result: ParseResult) -> list[dict[str, Any]]:
    """段落序列 → ``blocks[]``，同时算出全文档字符区间。

    ``FAILED`` 时 ``paragraphs`` 本就是空的，因此天然得到空列表 —— 不需要特判。
    """
    blocks: list[dict[str, Any]] = []
    cursor = 0

    for paragraph in parse_result.paragraphs:
        text = paragraph.text
        start = cursor
        end = start + len(text)
        blocks.append(
            {
                "order_index": paragraph.index,
                "paragraph_index": paragraph.index,
                "block_type": paragraph.block_type,
                "text": text,
                # ⚠️ P6 的 DocxParser 不保留归一化前的原文（只留 ``normalize_text`` 的结果），
                # 因此这一列当前填的就是归一化后的 text —— 它**不代表**真实的未归一化原文
                "raw_text": text,
                "char_start_in_block": 0,
                "char_end_in_block": len(text),
                "char_start_global": start,
                "char_end_global": end,
                # page_number / bbox_json / ocr_confidence 留空：DOCX 没有真实页码，
                # 也不走 OCR（§10.4 禁止估算填充）
            }
        )
        # 段落之间以 \n 分隔 —— 这正是 ParseResult.text 的拼接口径，
        # 因此 cursor 的推进必须 +1
        cursor = end + 1

    return blocks


def _block_positions(parse_result: ParseResult) -> dict[int, int]:
    """段落号 → 它在 ``blocks[]`` 里的下标。

    换算**必须**经过这张表（见模块 docstring 第 2 点）：Backend 要的是数组下标，
    而 Agent 的条款/元数据手里是段落号。两者今天恰好相等，但映射不该依赖这个巧合。
    """
    return {paragraph.index: position for position, paragraph in enumerate(parse_result.paragraphs)}


def _clause_payload(clause: Clause, positions: dict[int, int]) -> dict[str, Any]:
    """条款 → ``ClauseCreate``。

    ``clause_no`` / ``title`` 原样带上：Backend 的 ``clause`` 表有这两列，
    前端要显示「第三条 · 知识产权」。**丢弃它们等于让 Agent 故意扔掉接收方明确要的数据。**
    """
    start = _require_position(positions, clause.start_paragraph_index, what="条款起始段落")
    end = _require_position(positions, clause.end_paragraph_index, what="条款结束段落")
    if end < start:
        raise DocumentMappingError(
            f"条款区间倒置：start_paragraph_index={clause.start_paragraph_index} "
            f"大于 end_paragraph_index={clause.end_paragraph_index}（clause_index={clause.clause_index}）"
        )

    return {
        "clause_type": clause.clause_type,
        "text": clause.text,
        "extract_method": clause.extract_method,
        "clause_no": clause.clause_no,
        "title": clause.title,
        "start_block_index": start,
        "end_block_index": end,
    }


def _metadata_payload(item: MetadataItem, positions: dict[int, int]) -> dict[str, Any]:
    """元数据 → ``MetadataItemCreate``。

    ⚠️ ``MetadataItem.quote`` **不带**：Backend 的 ``contract_metadata`` 没有这一列，
    定位由 ``source_block_id`` 表达。这是已知的信息丢弃（P10-1 的映射清单即如此）。
    """
    return {
        "field_key": item.field_key,
        "field_label": item.field_label,
        "field_value": item.field_value,
        "value_type": item.value_type,
        "extract_method": item.extract_method,
        "source_block_index": _require_position(
            positions, item.paragraph_index, what=f"元数据 {item.field_key} 的来源段落"
        ),
    }


def _require_position(positions: dict[int, int], paragraph_index: int, *, what: str) -> int:
    """段落号 → ``blocks[]`` 下标；查不到就是本地产物自相矛盾，直接抛。"""
    position = positions.get(paragraph_index)
    if position is None:
        raise DocumentMappingError(
            f"{what} 引用了不存在的段落号 {paragraph_index}；"
            f"本次解析出的段落号为 {sorted(positions)}。"
            "这是解析/理解层的产物自相矛盾，拒绝生成越界的请求"
        )
    return position


# --------------------------------------------------------------------------- #
# 响应字段的防御性取值（与 contract_ingest / risk_persistence 同一口径）
# --------------------------------------------------------------------------- #
def _as_int(value: object) -> int | None:
    # bool 是 int 的子类，必须排除，否则 True 会被当成合法计数
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "DocumentMappingError",
    "DocumentPersistenceRequest",
    "DocumentPersistenceResult",
    "DocumentPersistenceTool",
]
