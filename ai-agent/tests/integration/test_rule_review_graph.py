"""``rule_review`` 在**真实 Graph** 里的接线（P8-2 第二小步）。

只 mock **网络层**（``httpx.MockTransport``），Agent 自己的节点全真实执行 ——
因此这里验证的是"图真的把规则审查接上了"，而不是"我 mock 的东西按我想的返回了"：

::

    upload_file → validate_file → parse_document → identify_clauses
                → extract_metadata → extract_keywords → rule_review
                → llm_review → merge_risks → END

文档用的是**真实 DOCX**（经「XML 样式 + 段落切分 + 条款识别」全链路），
规则用的是与 seed 同形的 3 条。节点的编解码细节由
``tests/unit/test_rule_review_node.py`` 负责，这里只回答"接上了没有"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.errors import AgentErrorCode
from app.graph.builder import NODE_RULE_REVIEW, build_review_graph
from app.graph.context import ReviewContext
from app.rules.schemas import AgentRule, RuleEvaluationStatus, RuleSetSnapshot
from app.tools.backend_client import BackendClient
from tests.factories import docx_bytes

BACKEND_BASE_URL = "http://backend.test"

SUCCESS_PAYLOAD: dict[str, Any] = {
    "contract_id": 11,
    "contract_no": "HT-2026-001",
    "title": "设备采购合同",
    "contract_type": "PURCHASE",
    "contract_status": "PENDING",
    "file_id": 22,
    "filename": "contract.docx",
    "file_size": 1234,
    "file_type": "DOCX",
    "sha256": "a" * 64,
    "parse_status": "PENDING",
    "review_task_id": 33,
    "task_status": "PENDING",
    "task_stage": "UPLOADED",
    "reused": False,
}

#: 一份真实 DOCX：第二条里写着 IP 归属相对方，第三条里没有违约关键词
DOCX_PARAGRAPHS = (
    "甲方：某某科技有限公司",
    "第一条 知识产权",
    "本项目产生的知识产权归乙方所有。",
    "第二条 违约责任",
    "乙方应在 10 日内完成整改。",
)
DOCX_BYTES = docx_bytes(*DOCX_PARAGRAPHS)

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]

IP_RULE = AgentRule(
    rule_code="IP_OWNER_SUPPLIER_001",
    rule_name="知识产权归属相对方",
    dimension="知识产权",
    rule_type="KEYWORD",
    expression={"keywords": ["知识产权归乙方"], "logic": "ANY"},
    target_clause_types=["IP"],
    severity="HIGH",
    sort_order=10,
)
LIAB_RULE = AgentRule(
    rule_code="LIAB_UNLIMITED_001",
    rule_name="我方单方承担无限责任",
    dimension="违约责任",
    rule_type="KEYWORD",
    expression={"keywords": ["全部损失"], "logic": "ANY"},
    target_clause_types=["LIABILITY"],
    severity="HIGH",
    sort_order=30,
)
THRESHOLD_RULE = AgentRule(
    rule_code="PAY_PREPAY_RATIO_001",
    rule_name="预付款比例超过 30%",
    dimension="金额支付",
    rule_type="THRESHOLD",
    expression={"field": "prepay_ratio", "op": "gt", "value": 0.3},
    target_clause_types=["AMOUNT_PAYMENT"],
    severity="MEDIUM",
    sort_order=50,
)

PURCHASE_SNAPSHOT = RuleSetSnapshot(
    contract_type="PURCHASE",
    rule_set_version="v1",
    rules=[IP_RULE, LIAB_RULE, THRESHOLD_RULE],
)

#: 节点模块对象 —— 用于把 ``evaluate_rule`` 换成 spy。
#: ⚠️ 不能用字符串 ``"app.graph.nodes.rule_review.evaluate_rule"``：``nodes/__init__.py``
#: 重导出了同名函数，那个名字在包命名空间里指的是**函数**而不是模块（见节点单测的说明）。
_RULE_REVIEW_MODULE = import_module("app.graph.nodes.rule_review")


@pytest.fixture
async def make_backend() -> AsyncIterator[Callable[[Handler], BackendClient]]:
    """挂了 MockTransport 的 BackendClient —— 与 ``test_minimal_review_graph.py`` 同一套。"""
    opened: list[httpx.AsyncClient] = []

    def _make(handler: Handler) -> BackendClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opened.append(client)
        return BackendClient(BACKEND_BASE_URL, client=client)

    yield _make

    for client in opened:
        await client.aclose()


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    path = tmp_path / "contract.docx"
    path.write_bytes(DOCX_BYTES)
    return path


def _initial_state(source_file: Path, snapshot: RuleSetSnapshot | None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "file_path": str(source_file),
        "filename": "contract.docx",
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "contract_no": "HT-2026-001",
        "title": "设备采购合同",
        "contract_type": "PURCHASE",
    }
    if snapshot is not None:
        state["rule_snapshot"] = snapshot
    return state


async def _run(backend: BackendClient, source_file: Path, snapshot: RuleSetSnapshot | None) -> dict[str, Any]:
    graph = build_review_graph()
    return await graph.ainvoke(
        _initial_state(source_file, snapshot),
        context=ReviewContext(backend=backend),
    )


def _ok_handler() -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=SUCCESS_PAYLOAD)

    return handler


# --------------------------------------------------------------------------- #
# 接线
# --------------------------------------------------------------------------- #
async def test_rule_review_is_wired_into_the_graph(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    graph = build_review_graph()

    assert NODE_RULE_REVIEW in graph.get_graph().nodes


async def test_graph_runs_rules_over_the_real_document(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """真实 DOCX → 条款 → 规则：命中的风险必须落回原文的段落上。"""
    final = await _run(make_backend(_ok_handler()), source_file, PURCHASE_SNAPSHOT)

    assert final["parse_result"].status == "PARSED"
    assert [e.status for e in final["rule_evaluations"]] == [
        RuleEvaluationStatus.MATCHED,
        RuleEvaluationStatus.NOT_MATCHED,
        RuleEvaluationStatus.EVALUATION_FAILED,
    ]

    (risk,) = final["rule_risks"]
    assert risk.risk_code == "IP_OWNER_SUPPLIER_001"
    assert risk.quote == "知识产权归乙方"
    # 定位能回到真实文档：段落序号有效，原文对得上
    paragraph = final["parse_result"].paragraphs[risk.paragraph_index]
    assert risk.original_text == paragraph.text


async def test_graph_actually_evaluates_every_rule(
    make_backend: Callable[[Handler], BackendClient], source_file: Path, monkeypatch
) -> None:
    """**证明规则真的被评估过** —— 光"图跑到了 END"说明不了任何事。

    把求值器换成记录调用的 spy：3 条规则就必须有 3 次调用，
    且每次拿到的都是本文档真实切出来的 clauses。
    """
    calls: list[tuple[str, list[Any]]] = []
    real_evaluate = _RULE_REVIEW_MODULE.evaluate_rule

    def spy(rule: Any, clauses: list[Any], metadata: Any = ()) -> Any:
        calls.append((rule.rule_code, clauses))
        return real_evaluate(rule, clauses, metadata=metadata)

    monkeypatch.setattr(_RULE_REVIEW_MODULE, "evaluate_rule", spy)

    final = await _run(make_backend(_ok_handler()), source_file, PURCHASE_SNAPSHOT)

    assert [code for code, _ in calls] == [
        "IP_OWNER_SUPPLIER_001",
        "LIAB_UNLIMITED_001",
        "PAY_PREPAY_RATIO_001",
    ]
    real_clauses = final["clauses"]
    assert real_clauses, "条款识别必须真的跑过"
    assert all(clauses == real_clauses for _, clauses in calls), "每条规则都拿到同一份真实条款"
    assert len(final["rule_evaluations"]) == 3


async def test_graph_keeps_the_evaluation_failed_rule_observable(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """THRESHOLD 算不出来（GAP-C）—— 在图跑完之后它依然在结果里，且不是 NOT_MATCHED。"""
    final = await _run(make_backend(_ok_handler()), source_file, PURCHASE_SNAPSHOT)

    failed = [e for e in final["rule_evaluations"] if e.status is RuleEvaluationStatus.EVALUATION_FAILED]
    assert [e.rule_code for e in failed] == ["PAY_PREPAY_RATIO_001"]
    assert failed[0].failure_message


async def test_graph_without_a_snapshot_is_a_workflow_failure(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """没给规则快照 = 输入缺失的失败：不抛异常，但 State 里必须留下 error，且没有任何结论。"""
    final = await _run(make_backend(_ok_handler()), source_file, None)

    assert final["error_code"] == AgentErrorCode.AGENT_INPUT_INVALID.value
    assert "rule_snapshot" in final["error_message"]
    assert "rule_evaluations" not in final
    assert "rule_risks" not in final, "失败时不能留下「0 条风险」这种可以被读成正常的痕迹"


async def test_graph_with_an_empty_rule_set_still_completes(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """``rule_set_version=None, rules=[]`` —— 正常结论，不是失败，也不生成风险。"""
    empty = RuleSetSnapshot(contract_type="SERVICE", rule_set_version=None, rules=[])

    final = await _run(make_backend(_ok_handler()), source_file, empty)

    assert final["rule_evaluations"] == []
    assert final["rule_risks"] == []
    assert final.get("error_code") is None, "没有规则集不是错误，不许写成 error"


async def test_upstream_nodes_still_run_before_rule_review(
    make_backend: Callable[[Handler], BackendClient], source_file: Path
) -> None:
    """规则审查接在既有主干之后，P7 的产出一个都不能少。"""
    final = await _run(make_backend(_ok_handler()), source_file, PURCHASE_SNAPSHOT)

    assert final["contract_id"] == 11
    assert final["clauses"], "条款识别必须已经跑过"
    assert final["keywords"], "关键词抽取必须已经跑过"
    assert any(clause.clause_type == "IP" for clause in final["clauses"])
