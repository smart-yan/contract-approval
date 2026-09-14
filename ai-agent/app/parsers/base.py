"""文档解析器的抽象。

Parser 的职责边界（只有一句话）
------------------------------
::

    文件  →  文档内容

**只做这一件事。** 它不认识合同条款、不提取元数据、不判断风险、不调 LLM ——
那些是 P7 及之后阶段的事。判据很简单：**"这个知识是关于文件格式的，还是关于合同的？"**
前者归 Parser，后者不归。

Parser 也**不访问 Backend、不碰数据库、不做文件校验与哈希**：
拿到的就是一个已经落好盘的本机路径（upload 环节交给 Backend 之后留在本地的那份）。

为什么失败用异常表达
------------------
:meth:`DocumentParser.parse` 抛 :class:`DocumentParseError` —— 那是 Parser **内部**
对"我做不到"的表达，对单独测试某个 Parser 来说它是最直接的失败信号。

但它**不会泄漏到 Node**：统一入口
:func:`app.parsers.parse_document_file` 会把它收敛成 ``ParseResult(status="FAILED")``，
于是 Node 与 Graph 只看结果，永远不需要 try/except 业务失败。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from app.core.errors import AgentErrorCode
from app.schemas.document import ParseResult


class DocumentParseError(Exception):
    """解析器无法完成本次解析。

    :param code: 稳定的错误码，最终会被写进 ``ParseResult.error_code`` 与 State
    """

    def __init__(self, code: AgentErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class DocumentParser(ABC):
    """文档解析器抽象。

    子类只需声明 :attr:`name` / :attr:`supported_file_types` 并实现 :meth:`parse`。
    """

    #: 解析器标识，写进 ``ParseResult.parser``，用于排查与统计
    name: ClassVar[str]

    #: 能处理的文件类型码（与 Backend ``contract_file`` 的 ``file_type`` 同一套取值）
    supported_file_types: ClassVar[frozenset[str]]

    @abstractmethod
    def parse(self, path: Path) -> ParseResult:
        """把本机文件解析成 :class:`ParseResult`。

        **同步阻塞**：解析库基本都是同步的，调用方负责把它放到线程池
        （架构文档 §1.3）。

        :raises DocumentParseError: 无法解析时
        """


__all__ = ["DocumentParseError", "DocumentParser"]
