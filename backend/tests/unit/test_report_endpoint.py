"""报告导出端点的传输层单元测试（**不需要 MySQL**）。

只测两件纯函数：下载文件名与 ``Content-Disposition`` 头。端点的端到端行为
（200/404/409、响应体是最终 Markdown）在 ``tests/integration/test_report_api.py``。

**中文文件名是本文件存在的主要理由。** HTTP 头的值只能是 latin-1，把中文直接
写进 ``filename="…"`` 会在编码环节炸掉或产出乱码 —— 这是"下载文件名乱码"这类
问题最常见的成因，而且**只有真的去编码那个头才会暴露**。因此这里有一条
``encode("latin-1")`` 的断言，专门守住它。
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import unquote

import pytest

from app.api.v1.endpoints.reports import (
    REPORT_MEDIA_TYPE,
    content_disposition,
    report_filename,
)
from app.services.report_render import (
    ReportContract,
    ReportData,
    ReportFile,
    ReportTask,
)


def _data(contract_no: str = "HT-2026-001", task_id: int = 42) -> ReportData:
    """只为文件名/响应头构造的最小 ReportData（这两个函数只看合同号与任务号）。"""
    return ReportData(
        task=ReportTask(
            task_id=task_id,
            status="pending",
            current_stage="REVIEWED",
            created_at=datetime(2026, 9, 15, 10, 0, tzinfo=UTC).replace(tzinfo=None),
        ),
        contract=ReportContract(
            contract_id=1,
            contract_no=contract_no,
            title="设备采购合同",
            contract_type="PURCHASE",
            our_party=None,
            counterparty=None,
            amount=None,
            currency=None,
            sign_date=None,
            effective_date=None,
            expire_date=None,
            dept=None,
        ),
        file=ReportFile(
            file_id=7, file_name="c.docx", file_type="DOCX", sha256="a" * 64, parse_status="PARSED"
        ),
        metadata=(),
        clauses=(),
        risks=(),
    )


# --------------------------------------------------------------------------- #
# media type
# --------------------------------------------------------------------------- #
def test_the_media_type_declares_utf8() -> None:
    """报告是中文文本；不声明编码会让部分客户端按 GBK 猜，整篇变乱码。"""
    assert REPORT_MEDIA_TYPE.startswith("text/markdown")
    assert "charset=utf-8" in REPORT_MEDIA_TYPE


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #
def test_the_filename_carries_the_contract_number_and_the_task_id() -> None:
    """带任务号是必要的：同一合同可以有多条审查任务，否则两个文件互相覆盖。"""
    assert report_filename(_data()) == "HT-2026-001-审查报告-42.md"


def test_the_filename_falls_back_when_the_contract_number_is_empty() -> None:
    assert report_filename(_data(contract_no="")) == "report-审查报告-42.md"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HT/2026/001", "HT-2026-001"),
        ("HT:2026*001", "HT-2026-001"),
        ('HT<>|?001', "HT----001"),
        # 路径穿越：``/`` 被替换成 ``-``，前导的 ``.`` 与 ``-`` 再被 strip 掉，
        # 因此 ``../../etc/passwd`` 不会变成任何有意义的相对路径
        ("../../etc/passwd", "etc-passwd"),
    ],
)
def test_illegal_filename_characters_are_replaced(raw: str, expected: str) -> None:
    """``contract_no`` 来自上传表单，**不是可信输入** —— 一个 ``/`` 就能让文件名变成路径。"""
    assert report_filename(_data(contract_no=raw)) == f"{expected}-审查报告-42.md"


# --------------------------------------------------------------------------- #
# Content-Disposition
# --------------------------------------------------------------------------- #
def _ascii_form(data: ReportData) -> str:
    """从 ``Content-Disposition`` 里取出 ASCII 兜底名。

    ⚠️ 必须用**同一个** ``data`` 同时生成文件名与响应头 —— 传两个不同的对象时
    兜底名会来自另一个合同号，测试却照样"通过"（这里踩过一次）。
    """
    disposition = content_disposition(report_filename(data), data)
    return disposition.split('filename="', 1)[1].split('"', 1)[0]


def test_the_disposition_is_an_attachment() -> None:
    assert content_disposition(report_filename(_data()), _data()).startswith("attachment;")


def test_the_header_is_latin1_encodable() -> None:
    """**这条是本文件的重点。**

    HTTP 头的值只能是 latin-1。把中文裸写进 ``filename="…"``，在真正把它编码成
    响应头时才会抛 ``UnicodeEncodeError`` —— 中文文件名最经典的翻车方式。
    """
    disposition = content_disposition(report_filename(_data()), _data())

    assert disposition.encode("latin-1")  # 不抛异常即通过


def test_the_header_carries_no_raw_chinese() -> None:
    disposition = content_disposition(report_filename(_data()), _data())

    assert "审查报告" not in disposition, "中文必须经过 percent-encoding，不能裸写"


def test_the_rfc5987_form_round_trips_to_the_chinese_name() -> None:
    """``filename*=UTF-8''…`` 解回来必须**逐字符**等于原文件名。"""
    filename = report_filename(_data())
    disposition = content_disposition(filename, _data())

    assert "filename*=UTF-8''" in disposition
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded) == filename


def test_the_ascii_fallback_name_is_pure_ascii_and_still_identifiable() -> None:
    """老客户端不认 ``filename*``，至少要能存下一个名字正确的文件，而不是乱码名。"""
    data = _data(contract_no="HT-2026-001")
    fallback = _ascii_form(data)

    assert fallback.isascii()
    assert fallback == "HT-2026-001-review-report-42.md"


def test_the_ascii_fallback_survives_a_fully_chinese_contract_number() -> None:
    data = _data(contract_no="采购合同")
    fallback = _ascii_form(data)

    assert fallback.isascii()
    assert fallback == "contract-review-report-42.md"


def test_both_filename_forms_are_present() -> None:
    """两个都要有：只给一个，总有一类客户端拿到乱码名或干脆存不下。"""
    disposition = content_disposition(report_filename(_data()), _data())

    assert 'filename="' in disposition
    assert "filename*=UTF-8''" in disposition
