"""理解层能力域（P7）。

把"文档"变成"关于文档的理解"：

* ``clauses`` —— 条款切分（P7-1，已实现）
* ``metadata`` —— 元数据抽取（P7-2，已实现）
* ``keywords`` —— 关键词抽取（P7-3，已实现）

这一层只依赖 ``schemas/document.py`` 的解析产物，不认识 State、不认识 Graph，
也不访问 Backend —— 因此每个能力都能脱离整张图单独测试，
且彼此**互不依赖**（都只吃 ``ParseResult``）。
"""

from app.understanding.clauses import classify_clause_type, identify_clauses
from app.understanding.keywords import KEYWORD_LEXICON, extract_keywords
from app.understanding.metadata import extract_metadata

__all__ = [
    "KEYWORD_LEXICON",
    "classify_clause_type",
    "extract_keywords",
    "extract_metadata",
    "identify_clauses",
]
