"""``parse_document`` 节点的职责：State → Parser → State。

这里只验证**编排**：从 State 取什么、往 State 写什么、失败怎么表达。
DOCX 内部的解析正确性由 ``test_docx_parser.py`` 负责 —— 节点测试不重复它。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AgentErrorCode
from app.graph.nodes.parse_document import parse_document
from tests.factories import save_docx

NODE_INPUT_PARAGRAPHS = (
    "甲方：某某科技有限公司",
    "乙方：另一家有限公司",
    "第一条 本合同自双方签字之日起生效。",
)


def _state(path: Path, file_type: str = "DOCX") -> dict:
    return {"file_path": str(path), "file_type": file_type, "file_id": 42}


# --------------------------------------------------------------------------- #
# 正常路径：ParseResult 写回 State
# --------------------------------------------------------------------------- #
async def test_node_writes_parse_result_into_state(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "contract.docx", *NODE_INPUT_PARAGRAPHS)

    updates = await parse_document(_state(path))

    assert set(updates) == {"parse_result"}, "成功路径只写 parse_result，不污染其它 State 字段"

    result = updates["parse_result"]
    assert result.status == "PARSED"
    assert result.parser == "DocxParser"
    assert len(result.paragraphs) == 3
    assert "某某科技有限公司" in result.text


async def test_node_does_not_touch_input_fields(tmp_path: Path) -> None:
    """节点只写自己负责的键 —— 不去改上游留下的 file_path / file_id。"""
    path = save_docx(tmp_path / "c.docx", "正文")

    updates = await parse_document(_state(path))

    assert "file_path" not in updates
    assert "file_id" not in updates
    assert "file_type" not in updates


async def test_empty_document_is_not_an_error(tmp_path: Path) -> None:
    path = save_docx(tmp_path / "empty.docx")

    updates = await parse_document(_state(path))

    assert set(updates) == {"parse_result"}
    assert updates["parse_result"].status == "EMPTY"


# --------------------------------------------------------------------------- #
# 失败路径：写进 State，而不是抛异常
# --------------------------------------------------------------------------- #
async def test_unsupported_file_type_is_written_into_state(tmp_path: Path) -> None:
    """PDF 现在还没接 —— 结果要明确说明"不支持"，而不是静默给个空文档。"""
    path = save_docx(tmp_path / "whatever.docx", "内容")
    state = _state(path, file_type="PDF")

    updates = await parse_document(state)

    assert updates["parse_result"].status == "FAILED"
    assert updates["error_code"] == AgentErrorCode.PARSE_UNSUPPORTED_TYPE.value
    assert "PDF" in (updates["error_message"] or "")


async def test_missing_file_is_written_into_state_not_raised(tmp_path: Path) -> None:
    """解析失败绝不能中断整张图 —— 失败是结果，交给下游判断。"""
    updates = await parse_document(_state(tmp_path / "not-there.docx"))

    assert updates["parse_result"].status == "FAILED"
    assert updates["error_code"] == AgentErrorCode.PARSE_FAILED.value
    assert updates["parse_result"].text == ""


async def test_missing_file_path_is_reported_without_touching_the_parser(tmp_path: Path) -> None:
    updates = await parse_document({"file_type": "DOCX"})

    assert updates["error_code"] == AgentErrorCode.AGENT_INPUT_INVALID.value
    assert "file_path" not in updates, "输入不完整时不该产出 parse_result"


@pytest.mark.parametrize("bad_state", [{}, {"file_path": "", "file_type": "DOCX"}])
async def test_node_never_raises_on_bad_input(bad_state: dict) -> None:
    updates = await parse_document(bad_state)

    assert updates["error_code"] == AgentErrorCode.AGENT_INPUT_INVALID.value
