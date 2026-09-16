"""异常路径演示脚本（P16-3）。

⚠️ **这不是测试**：它**不**跑在 pytest 里、**不**做断言、**不**集成进 CI。
它是一个**开发环境工具**：把 6 类 P15/P16 已经冻结的异常 / 恢复路径端到端跑一遍，
打印人类可读报告，方便面试演示 / 自查 / 排查。

与现有测试的关系
----------------
本脚本**不**从 ``backend/tests/`` 导入 fixture —— 那些 fixture 是测试私有物，
不应该被 demo 脚本耦合。本脚本自带 4 个最小化的 Demo ApprovalClient：

* ``DemoTransportBroken`` —— 模拟网络故障（``post_comment`` 抛 ``ApprovalSystemError``）
* ``DemoLostResponse`` —— 模拟"外部写成功、响应丢失"（先真写再抛超时）
* ``DemoRejecting`` —— 模拟外部业务拒绝（``post_comment`` 抛 ``AppError``）
* ``DemoCounting`` —— 计数 ``find_comment`` / ``post_comment`` 调用次数

它们对应的生产代码在 ``app/services/writeback_execution.py`` 和
``app/integrations/mock_approval.py`` —— 这是项目里**唯一**用来"换 ApprovalClient"
演示异常的方式。

数据隔离
--------
所有 demo 数据使用 ``P16-DEMO-`` 前缀：

* ``contract_no`` = ``P16-DEMO-{uuid}``
* ``instance_no`` = ``P16-DEMO-{uuid}``

清理顺序（按子 → 父）：

1. ``approval_comment``       (FK → approval_instance)
2. ``writeback_record``       (FK → review_task / contract / approval_instance)
3. ``risk_item``              (FK → review_task / contract / clause)
4. ``clause``                 (FK → review_task / contract)
5. ``contract_metadata``      (FK → review_task)
6. ``document_block``         (FK → contract / contract_file)
7. ``contract_file``          (FK → contract)
8. ``review_task``            (FK → contract / contract_file)
9. ``contract``               (FK → approval_instance 可空；反向有 approval_instance.contract_id)
10. ``approval_instance``     (无 FK 出，被 contract.contract_id 引用 —— 必须先删合同)

try / finally 双保险：脚本任何异常都尝试清理再退出。

⚠️ **绝不**走 ``DELETE FROM xxx;`` 这类无前缀清空 —— 那会污染生产数据。

运行环境
--------

::

    cd backend
    uv run python scripts/demo_writeback_failures.py
    # 或：
    cd ..
    uv run --project . python backend/scripts/demo_writeback_failures.py
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

# 允许 `python scripts/demo_writeback_failures.py` 直接跑（与 seed_mock_approval.py 同一手法）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.errors import ErrorCode, NotFoundError
from app.db.session import dispose_engine, session_scope
from app.integrations.approval import ApprovalClient, ApprovalCommentRef, ApprovalSystemError
from app.integrations.mock_approval import MockApprovalClient
from app.services.writeback_execution import execute_writeback

#: 所有 demo 数据前缀
PREFIX = "P16-DEMO-"

#: 锁前缀
_uuid = uuid.uuid4().hex[:8]


def _u() -> str:
    """每次调用返回一个 8 位 hex 后缀，避免同一次脚本运行内出现 ID 冲突。"""
    return uuid.uuid4().hex[:8]


# =========================================================================== #
# Demo 专用 ApprovalClient —— 不复用 tests/integration 里的私有 fixture
# =========================================================================== #
class DemoTransportBroken(ApprovalClient):
    """模拟"审批系统不可达"——``post_comment`` 直接抛 ``ApprovalSystemError``。

    这是 **transport failure** 的最小复现：**不**接真实网络，**不**停 MySQL。
    ``ApprovalSystemError`` 是项目为"外部调用层失败"专门定义的 RuntimeError
    （见 ``app/integrations/approval.py`` 的设计说明）。
    """

    async def find_comment(self, instance_id: int, idempotency_key: str) -> ApprovalCommentRef | None:
        return None

    async def post_comment(self, instance_id: int, content_md: str, idempotency_key: str) -> ApprovalCommentRef:
        raise ApprovalSystemError("审批系统不可达（demo 模拟 transport failure）")


class DemoLostResponse(ApprovalClient):
    """模拟"外部写成功、但响应丢了"。

    第 1 次 ``post_comment``：先用真实 ``MockApprovalClient`` 把评论落库，
    然后**主动抛** ``ApprovalSystemError("响应丢失")`` —— 调用方看到的是失败，
    而外部其实已经有了这条评论（§14.3 的 ``duplicate_comment`` 故障注入场景）。
    后续 ``post_comment`` 恢复正常行为，让 retry 能直接走 find_comment 恢复。
    """

    def __init__(self) -> None:
        self._real = MockApprovalClient()
        self.lost_once = False

    async def find_comment(self, instance_id: int, idempotency_key: str) -> ApprovalCommentRef | None:
        return await self._real.find_comment(instance_id, idempotency_key)

    async def post_comment(self, instance_id: int, content_md: str, idempotency_key: str) -> ApprovalCommentRef:
        # 真的把评论写进 Mock —— 这是"响应丢失"的本体：外部落库了，但调用方看不到
        ref = await self._real.post_comment(instance_id, content_md, idempotency_key)
        if not self.lost_once:
            self.lost_once = True
            raise ApprovalSystemError("响应丢失（demo 模拟 lost response）")
        return ref


class DemoRejecting(ApprovalClient):
    """模拟"外部业务拒绝"——``post_comment`` 抛 ``AppError``（非网络错误）。

    这是 Backend 的 ``ApprovalClient`` 契约：业务性失败用 ``AppError``，调用方
    ``writeback_execution.execute_writeback`` 会先写本地 ``FAILED``，**再原样
    抛出**给调用方（不吞掉）。Mock 实现（``mock_approval.py``）会在审批单行不存在
    时抛 ``NotFoundError``——这里复现同一语义。
    """

    async def find_comment(self, instance_id: int, idempotency_key: str) -> ApprovalCommentRef | None:
        return None

    async def post_comment(self, instance_id: int, content_md: str, idempotency_key: str) -> ApprovalCommentRef:
        raise NotFoundError(
            f"审批单 {instance_id} 不存在，无法写入评论（demo 模拟业务拒绝）",
            code=ErrorCode.NOT_FOUND,
            details={"approval_instance_id": instance_id},
        )


class DemoCounting(ApprovalClient):
    """包裹一个真实 Client，计数 ``find_comment`` / ``post_comment`` 调用次数。

    用于断言"是否真的发了写请求"——例如 posted=false 路径应该 find=1、post=0。
    """

    def __init__(self, inner: ApprovalClient) -> None:
        self._inner = inner
        self.finds = 0
        self.posts = 0

    async def find_comment(self, instance_id: int, idempotency_key: str) -> ApprovalCommentRef | None:
        self.finds += 1
        return await self._inner.find_comment(instance_id, idempotency_key)

    async def post_comment(self, instance_id: int, content_md: str, idempotency_key: str) -> ApprovalCommentRef:
        self.posts += 1
        return await self._inner.post_comment(instance_id, content_md, idempotency_key)


# =========================================================================== #
# 数据准备（每个场景独立数据集）
# =========================================================================== #
@dataclass(frozen=True)
class Scene:
    """一次 demo 场景的最小数据集。"""

    contract_id: int
    task_id: int
    instance_id: int


async def _seed_one_scene(label: str) -> Scene:
    """造一份最小 REVIEWED 场景：合同 + 附件 + ReviewTask(REVIEWED) + 风险 + 审批单。"""
    suffix = f"{label}-{_u()}"
    instance_no = f"{PREFIX}{suffix}"
    contract_no = f"{PREFIX}{suffix}"

    async with session_scope() as session:
        # 1) 审批单 —— 不写 contract_id（避免 FK 让合同无法删除，详见 seed_mock_approval）
        r = await session.execute(
            text(
                "INSERT INTO approval_instance "
                "(instance_no, title, applicant, dept, amount, status, contract_id, created_at, updated_at) "
                "VALUES (:no, :title, :applicant, :dept, :amount, 'PENDING', NULL, NOW(3), NOW(3))"
            ),
            {
                "no": instance_no,
                "title": f"P16-3 demo {label}",
                "applicant": "demo",
                "dept": "demo",
                "amount": Decimal("100.00"),
            },
        )
        instance_id = r.lastrowid

        # 2) 合同 → 绑定到该审批单
        r = await session.execute(
            text(
                "INSERT INTO contract "
                "(contract_no, title, contract_type, source, status, approval_instance_id, created_at, updated_at) "
                "VALUES (:no, :title, 'PURCHASE', 'UPLOAD', 'PENDING', :ap, NOW(3), NOW(3))"
            ),
            {"no": contract_no, "title": f"P16-3 demo {label}", "ap": instance_id},
        )
        contract_id = r.lastrowid

        # 3) 合同附件
        sha = hashlib.sha256(f"p16-demo-{suffix}".encode()).hexdigest()
        r = await session.execute(
            text(
                "INSERT INTO contract_file "
                "(contract_id, file_name, file_ext, file_size, sha256, storage_path, is_scanned, parse_status, "
                " created_at, updated_at) "
                "VALUES (:c, :name, '.docx', 1024, :sha, :path, 0, 'PARSED', NOW(3), NOW(3))"
            ),
            {"c": contract_id, "name": f"{label}.docx", "sha": sha, "path": f"{contract_id}/{label}.docx"},
        )
        file_id = r.lastrowid

        # 4) ReviewTask（**REVIEWED**——写回门禁要求的阶段）
        r = await session.execute(
            text(
                "INSERT INTO review_task "
                "(contract_id, file_id, status, current_stage, progress, priority, version, retry_count, "
                " max_retry, idempotency_key, created_at, updated_at) "
                "VALUES (:c, :f, 'pending', 'REVIEWED', 100, 0, 0, 0, 3, :k, NOW(3), NOW(3))"
            ),
            {"c": contract_id, "f": file_id, "k": hashlib.sha256(f"task-{suffix}".encode()).hexdigest()},
        )
        task_id = r.lastrowid

        # 5) 一条风险（P15-1 渲染需要）
        await session.execute(
            text(
                "INSERT INTO risk_item "
                "(task_id, contract_id, clause_id, risk_code, risk_title, dimension, risk_level, source, "
                " reason, legal_basis, original_text, paragraph_index, locator_type, review_status, "
                " created_at, updated_at) "
                "VALUES (:t, :c, NULL, 'P16_DEMO', :title, 'demo', 'HIGH', 'RULE', 'reason', 'basis', "
                "        'quote', 1, 'PARAGRAPH', 'PENDING', NOW(3), NOW(3))"
            ),
            {"t": task_id, "c": contract_id, "title": f"{label} demo risk"},
        )

    return Scene(contract_id=contract_id, task_id=task_id, instance_id=instance_id)


# =========================================================================== #
# Cleanup
# =========================================================================== #
async def _purge_demo_data() -> None:
    """按 FK 顺序清理 ``P16-DEMO-`` 前缀的所有数据。

    清理顺序（子表 → 父表）：

    1. ``approval_comment``       (FK → approval_instance)
    2. ``writeback_record``       (FK → review_task)
    3. ``risk_item``              (FK → review_task)
    4. ``contract_metadata``      (FK → review_task)
    5. ``review_task``            (FK → contract)
    6. ``contract_file``          (FK → contract)
    7. ``contract``               (FK → approval_instance 可空)
    8. ``approval_instance``     (无 FK 出，被 contract.contract_id 引用 —— 必须先删合同)

    ⚠️ **绝不**走 ``DELETE FROM xxx;`` 这类无前缀清空 —— 那会污染生产数据。
    """
    async with session_scope() as session:
        # 先收集 demo 范围内的 id 集合，避免误删
        instance_rows = (
            await session.execute(
                text("SELECT id FROM approval_instance WHERE instance_no LIKE :p"),
                {"p": f"{PREFIX}%"},
            )
        ).all()
        instance_ids = [row[0] for row in instance_rows]

        contract_rows = (
            await session.execute(
                text("SELECT id FROM contract WHERE contract_no LIKE :p"), {"p": f"{PREFIX}%"}
            )
        ).all()
        contract_ids = [row[0] for row in contract_rows]

        if not instance_ids and not contract_ids:
            return  # 没有 demo 数据，无需清理

        # 先用 contract_ids 拿 task_ids（writeback_record / risk_item / clause / contract_metadata 都 FK task）
        if contract_ids:
            task_rows = (
                await session.execute(
                    text("SELECT id FROM review_task WHERE contract_id IN :c"),
                    {"c": tuple(contract_ids)},
                )
            ).all()
            task_ids = [row[0] for row in task_rows]
        else:
            task_ids = []

        def _in_clause(ids: list[int]) -> tuple[str, dict[str, Any]]:
            ph = ",".join([f":x{n}" for n in range(len(ids))])
            params: dict[str, Any] = {f"x{n}": v for n, v in enumerate(ids)}
            return ph, params

        if instance_ids:
            ph, params = _in_clause(instance_ids)
            await session.execute(text(f"DELETE FROM approval_comment WHERE instance_id IN ({ph})"), params)

        if task_ids:
            ph, params = _in_clause(task_ids)
            await session.execute(text(f"DELETE FROM writeback_record WHERE task_id IN ({ph})"), params)
            await session.execute(text(f"DELETE FROM risk_item WHERE task_id IN ({ph})"), params)
            await session.execute(text(f"DELETE FROM contract_metadata WHERE task_id IN ({ph})"), params)
            await session.execute(text(f"DELETE FROM review_task WHERE id IN ({ph})"), params)

        if contract_ids:
            ph, params = _in_clause(contract_ids)
            await session.execute(text(f"DELETE FROM contract_file WHERE contract_id IN ({ph})"), params)
            await session.execute(text(f"DELETE FROM contract WHERE id IN ({ph})"), params)

        # approval_instance 必须在 contract 之后（contract.contract_id 是反向 FK，
        # 但更稳的写法：先清掉反向引用 —— 我们建 demo instance 时 contract_id=NULL，
        # 所以直接删 instance 即可）
        if instance_ids:
            ph, params = _in_clause(instance_ids)
            await session.execute(text(f"DELETE FROM approval_instance WHERE id IN ({ph})"), params)


# =========================================================================== #
# Demo 主流程
# =========================================================================== #
def _section(title: str) -> None:
    print()
    print(f"──── {title} ────")


def _kv(label: str, value: object) -> None:
    print(f"      {label:<20} {value}")


async def demo_transport_failure() -> None:
    """[1/6] Transport failure → FAILED.

    验证：``DemoTransportBroken`` 抛 ``ApprovalSystemError`` 后，本地
    ``writeback_record.status`` 落 ``FAILED`` + ``error_msg`` 有值 + 外部评论**没**写。
    """
    _section("[1/6] Transport failure → FAILED")
    scene = await _seed_one_scene("sc1")

    client = DemoTransportBroken()
    result = await execute_writeback(scene.task_id, client=client)

    _kv("status", result.status)
    _kv("error_msg", result.error_msg)
    _kv("external_comment_id", result.external_comment_id)
    _kv("approval_instance_id", result.approval_instance_id)

    # 验证外部评论真的没写
    async with session_scope() as session:
        r = await session.execute(
            text("SELECT COUNT(*) FROM approval_comment WHERE instance_id = :i"), {"i": scene.instance_id}
        )
        count = r.scalar()
    _kv("external comments", count)


async def demo_failed_then_retry() -> None:
    """[2/6] FAILED → retry → SUCCESS.

    验证：第一次用 ``DemoTransportBroken`` 拿到 FAILED；第二次用真实
    ``MockApprovalClient`` 重试——状态机允许 ``FAILED → WRITING``（attempt+1），
    最终 ``success``。
    """
    _section("[2/6] FAILED → retry → SUCCESS")
    scene = await _seed_one_scene("sc2")

    # 第一次：transport broken → FAILED
    first = await execute_writeback(scene.task_id, client=DemoTransportBroken())
    _kv("first.status", first.status)
    _kv("first.error_msg", first.error_msg)
    _kv("first.attempt", first.attempt)

    # 第二次：换真实 client → SUCCESS
    second = await execute_writeback(scene.task_id, client=MockApprovalClient())
    _kv("retry.status", second.status)
    _kv("retry.attempt", second.attempt)
    _kv("retry.external_comment_id", second.external_comment_id)


async def demo_existing_external_comment() -> None:
    """[3/6] 外部已有相同 idempotency_key → posted=false.

    验证：先正常写一次成功；然后**手动 DELETE writeback_record**（模拟"记录丢了"），
    再跑一次——``find_comment`` 命中，**不 post**，直接 flip 本地为 SUCCESS，``posted=False``。
    """
    _section("[3/6] External already has this comment → posted=false")
    scene = await _seed_one_scene("sc3")

    # 第一次：正常成功
    first = await execute_writeback(scene.task_id, client=MockApprovalClient())
    _kv("first.status", first.status)
    _kv("first.external_comment_id", first.external_comment_id)

    # 模拟"记录丢了"
    async with session_scope() as session:
        await session.execute(text("DELETE FROM writeback_record WHERE task_id = :t"), {"t": scene.task_id})

    # 第二次：用 counting client 验证 find=1 / post=0
    counting = DemoCounting(MockApprovalClient())
    second = await execute_writeback(scene.task_id, client=counting)

    _kv("second.status", second.status)
    _kv("second.posted", second.posted)
    _kv("second.external_comment_id", second.external_comment_id)
    _kv("find_comment calls", counting.finds)
    _kv("post_comment calls", counting.posts)

    # 外部评论仍然只有一条
    async with session_scope() as session:
        r = await session.execute(
            text("SELECT COUNT(*) FROM approval_comment WHERE instance_id = :i"), {"i": scene.instance_id}
        )
        count = r.scalar()
    _kv("external comments", count)


async def demo_already_success() -> None:
    """[4/6] SUCCESS → 再次调用 → 409 WRITEBACK_ALREADY_SUCCESS.

    验证：第一次成功之后第二次再调，本地 ``writeback_record.status==success``
    触发 ``ConflictError(code=WRITEBACK_ALREADY_SUCCESS)``。这是"幂等键保护
    终态"的语义：成功后不再产生第二条评论。
    """
    _section("[4/6] SUCCESS → 再调用 → 409 WRITEBACK_ALREADY_SUCCESS")
    scene = await _seed_one_scene("sc4")

    # 第一次成功
    first = await execute_writeback(scene.task_id, client=MockApprovalClient())
    _kv("first.status", first.status)

    # 第二次 —— 应当抛 ConflictError
    caught_code: str | None = None
    caught_http: int | None = None
    try:
        await execute_writeback(scene.task_id, client=MockApprovalClient())
    except Exception as exc:  # noqa: BLE001
        caught_code = getattr(exc, "code", None)
        caught_http = getattr(exc, "http_status", None)

    _kv("caught.code", caught_code)
    _kv("caught.http_status", caught_http)

    # 外部评论仍然只有一条
    async with session_scope() as session:
        r = await session.execute(
            text("SELECT COUNT(*) FROM approval_comment WHERE instance_id = :i"), {"i": scene.instance_id}
        )
        count = r.scalar()
    _kv("external comments", count)


async def demo_external_business_rejection() -> None:
    """[5/6] 外部业务拒绝 → FAILED + AppError 原样抛出.

    验证：``DemoRejecting`` 抛 ``NotFoundError`` 后，本地先落 ``FAILED``（不让记录
    停在 ``WRITING``），然后**原样抛出**给调用方——调用方必须能看到业务错误（4xx），
    而不是被静默改写成"success"。
    """
    _section("[5/6] External business rejection → FAILED + AppError")
    scene = await _seed_one_scene("sc5")

    caught_code: str | None = None
    caught_http: int | None = None
    try:
        await execute_writeback(scene.task_id, client=DemoRejecting())
    except Exception as exc:  # noqa: BLE001
        caught_code = getattr(exc, "code", None)
        caught_http = getattr(exc, "http_status", None)

    _kv("caught.code", caught_code)
    _kv("caught.http_status", caught_http)

    # 本地状态必须是 FAILED（不是 WRITING）
    async with session_scope() as session:
        r = await session.execute(
            text("SELECT status, error_msg FROM writeback_record WHERE task_id = :t"), {"t": scene.task_id}
        )
        row = r.first()
    _kv("writeback.status", row[0] if row else None)
    _kv("writeback.error_msg", row[1] if row else None)


async def demo_agent_block() -> None:
    """[6/6] Agent graph exception → BLOCKED.

    ⚠️ **不动 Agent production code**：直接通过 Backend 的
    ``POST /api/v1/review-tasks/{task_id}/block`` 上报 Agent 的失败信号——这正是
    P14-4 的 ``Agent._report_blocked`` 内部做的事（``ai-agent/app/api/review.py:250-266``）。

    验证：``block_reason_code == 'AGENT_GRAPH_EXECUTION_FAILED'`` + ``status == 'blocked'``
    + ``current_stage`` **不**前不**后**（P14-5-2 冻结语义）。
    """
    _section("[6/6] Agent graph exception → BLOCKED")
    scene = await _seed_one_scene("sc6")

    # 记录原始 stage，用于"不推进也不回退"断言
    async with session_scope() as session:
        r = await session.execute(
            text("SELECT current_stage FROM review_task WHERE id = :t"), {"t": scene.task_id}
        )
        original_stage = r.scalar()

    # 直接调 Backend 的 task_block service —— 这就是 Agent 在 P14-4 内部做的事
    from app.schemas.task import TaskBlockRequest  # 延迟 import
    from app.services.task_block import block_task  # 延迟 import，避免循环

    block_resp = await block_task(
        scene.task_id,
        TaskBlockRequest(
            block_reason_code="AGENT_GRAPH_EXECUTION_FAILED",
            block_reason_msg="图执行抛 RuntimeError: 图炸了（demo 模拟）",
        ),
    )

    _kv("new.status", block_resp.status)
    _kv("new.current_stage", block_resp.current_stage)
    _kv("new.block_reason_code", block_resp.block_reason_code)

    # 阶段断言：与原始一致（不前不后）
    async with session_scope() as session:
        r = await session.execute(
            text("SELECT current_stage, status, block_reason_code, block_reason_msg FROM review_task WHERE id = :t"),
            {"t": scene.task_id},
        )
        row = r.first()
    _kv("db.status", row[1])
    _kv("db.current_stage (was " + str(original_stage) + ")", row[0])
    _kv("db.block_reason_code", row[2])
    _kv("db.block_reason_msg", row[3])


# =========================================================================== #
# 入口
# =========================================================================== #
async def main() -> int:
    print("[P16-3] Writeback Failure Demo")
    print(f"[P16-3] data prefix: {PREFIX}*  (isolated from production)")
    demos: list[tuple[str, Any]] = [
        ("transport failure", demo_transport_failure),
        ("FAILED -> retry -> SUCCESS", demo_failed_then_retry),
        ("existing external comment", demo_existing_external_comment),
        ("ALREADY_SUCCESS 409", demo_already_success),
        ("external business rejection", demo_external_business_rejection),
        ("agent graph exception -> BLOCKED", demo_agent_block),
    ]

    try:
        for label, fn in demos:
            try:
                await fn()
            except Exception as exc:  # noqa: BLE001
                print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")
                # 继续下一个场景 —— 一个失败不毁掉其他演示
    finally:
        print()
        print("[P16-3] Cleanup ...")
        try:
            await _purge_demo_data()
            print("[P16-3] Cleanup completed")
        except Exception as exc:  # noqa: BLE001
            print(f"[P16-3] Cleanup failed: {type(exc).__name__}: {exc}")
            print(f"[P16-3] ⚠️ 手动清理：DELETE WHERE instance_no LIKE '{PREFIX}%' 等")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))