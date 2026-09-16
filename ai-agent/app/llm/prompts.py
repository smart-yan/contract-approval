"""版本化提示：文本在 ``prompts/*.md`` 里，**版本写在文件名上**（§9.3）。

为什么提示是文件而不是字符串常量
------------------------------
提示会被反复改。把它放进代码里，改动就混在业务逻辑的 diff 里；写成带版本号的
独立文件，"改了 prompt 之后效果变好还是变差"才有据可查：
``prompt_version`` 会进 ``ai_call_log``，也进 ``review_task.idempotency_key``。

**改提示一律新建版本文件**（``clause_review.v2.md``），不覆盖旧文件 ——
覆盖会让历史任务的"当时用的是什么提示"永远查不回来。

与 json_guard 的分工
------------------
本模块只提供**业务口径的措辞**（该做什么、不该做什么）。
"只输出 JSON / 结构如下"这类**机械约束**由 :func:`~app.llm.json_guard.build_schema_instruction`
从 Pydantic 模型生成并追加 —— 提示文件里**不抄一遍 JSON Schema**：
抄本一定会与真正校验的模型漂移，而且不会有人发现。
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from app.llm.findings import ClauseReviewPromptInput

#: ``clause_review`` 场景的当前提示版本 —— 同时也是文件名（不含扩展名）。
#: 它会被写进 ``LLMRequest.prompt_version``，因此**改文件名就是改版本**。
#:
#: ⚠️ **v1 已停用但仍然留在仓库里**（这是本项目的规矩：改提示一律新建版本文件，
#: 不覆盖旧的 —— 覆盖会让"历史任务当时用的是哪版提示"永远查不回来）。
#: 它现在**跑不通**了：P9-8a 之后 finding 契约要求 ``dimension``，
#: 而 v1 的正文里没有关于它的任何说明，模型不会输出该字段 → 校验必然失败。
#: 想复现 v1 的行为，得同时回到当时的 finding 契约（去掉 dimension）。
PROMPT_CLAUSE_REVIEW_V1 = "clause_review.v1"

#: v2：输出契约里增加了必填的 ``dimension``（P9-8a）。**已停用，但留在仓库里。**
PROMPT_CLAUSE_REVIEW_V2 = "clause_review.v2"

#: 当前版本：``context_before`` / ``context_after`` 由「必须输出」改为
#: 「**可选，默认省略**」（P14-3-5）。
#:
#: 为什么改：真实 DeepSeek 调用（P14-3-2/3-4）证明模型给出的 context **永远**取自
#: **相邻的另一个段落**（上一项条款 / 条款标题 / 表格其它行），而定位器要求的是
#: **同一段落内、紧贴 quote 的字符级前后缀**。实测 6/6 条：提供 context 会把定位
#: 从 ``CLAUSE_SCOPED`` **降级**为 ``CLAUSE_FALLBACK``，不提供则全部精确命中。
#: 因此不再要求模型生成它 —— 字段与定位器的支持**都保留**，只是默认留空。
PROMPT_CLAUSE_REVIEW_V3 = "clause_review.v3"

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


@cache
def load_prompt(name: str) -> str:
    """读取提示文件并返回其正文（去掉首尾空白）。

    :param name: 提示名**含版本**，如 ``clause_review.v1``
    :raises FileNotFoundError: 文件不存在 —— 这是**打包/部署错误**，必须响亮地失败，
        不能悄悄回落成一句空提示（那样模型会得到一份没有约束的系统提示，
        而我们会以为一切正常）
    """
    path = _PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"提示文件不存在：{path}（版本即文件名，改提示请新建版本文件）")
    return path.read_text(encoding="utf-8").strip()


def render_clause_review_user_prompt(payload: ClauseReviewPromptInput) -> str:
    """把一批条款与已命中规则渲染成 **user prompt**。

    纯机械排版：怎么把结构化输入排成人能读（模型也能读）的文本。
    **业务口径的措辞不在这里** —— 那是 ``prompts/clause_review.<版本>.md`` 的事。

    渲染是确定性的：同一份输入永远得到同一段文本（便于回归对比与排障）。
    """
    lines: list[str] = [f"【合同类型】{payload.contract_type}", ""]

    lines.append(f"【本批条款】（共 {len(payload.clauses)} 条，clause_index 请原样回显）")
    for clause in payload.clauses:
        heading = " ".join(part for part in (clause.clause_no, clause.title) if part)
        label = f"{heading}·{clause.clause_type}" if heading else clause.clause_type
        lines.append(f"--- 条款 #{clause.clause_index}（{label}）---")
        lines.append(clause.text)
        lines.append("")

    if payload.matched_rules:
        lines.append("【规则已命中】（不要重复报告；相关的请在 related_rule_code 里回填对应编码）")
        for rule in payload.matched_rules:
            lines.append(
                f"- rule_code={rule.rule_code} | {rule.rule_name} | 维度={rule.dimension} "
                f"| 等级={rule.risk_level} | 命中证据：{rule.quote}"
            )
    else:
        lines.append("【规则已命中】（无）")

    return "\n".join(lines).strip()


__all__ = [
    "PROMPT_CLAUSE_REVIEW_V1",
    "PROMPT_CLAUSE_REVIEW_V2",
    "PROMPT_CLAUSE_REVIEW_V3",
    "load_prompt",
    "render_clause_review_user_prompt",
]
