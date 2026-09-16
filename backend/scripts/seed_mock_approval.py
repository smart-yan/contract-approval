"""灌入 Mock 审批域的确定性数据（架构文档 §14.4、§15；P15-3a）。

职责边界
-------
本脚本**只写 Mock 审批域的基础数据**：4 张 ``approval_instance``，
并把**已存在的**合同与它们建立关联（``contract.approval_instance_id``）。它

* **不实现** Mock 审批的接口（待办拉取 / 附件下载 / 评论写回）—— 那些属于后续步骤；
* **不创建**合同、附件或审查任务 —— 合同由真实的接入流程产生（上传 → 解析 → 审查），
  seed 只负责"审批单这一侧"，不伪造业务数据；
* **不改** ``approval_comment`` 的既有数据。

为什么是这 4 张审批单
--------------------
§14.4 点名要 4 张，并给出各自的演示场景。这里逐字沿用：

= ============== ==========================================
1 软件采购合同    **端到端主演示场景**（知识产权归属供应商）
2 大额销售合同    违约责任不对等
3 劳动合同        中风险场景
4 扫描件采购合同  OCR 链路场景
= ============== ==========================================

⚠️ 标题/申请人/金额都是**演示用的 Mock 数据**（Mock 域按 §7.2 就用纯字符串，
不关联 ``sys_user``）。它们不是从任何真实来源推导的，也不该被当成业务事实 ——
真实的审批单将来由"待办拉取"从 Mock（未来是真实）审批系统同步进来。

绑定关系怎么定（不假定任何 Contract ID）
------------------------------------
每张审批单对应一份**样例合同文件**（§14.2 的"附件下载"返回到 ``samples/``）。
因此绑定规则是**可复现的**：样例文件的 SHA-256 是固定的，而
``contract_file.sha256`` 全局 UNIQUE —— 于是"哪份合同属于这张审批单"
由**文件内容**唯一确定，与自增 ID 无关：

::

    samples/<样例文件> → sha256 → contract_file.sha256 → contract_id
                                                            │
                                            contract.approval_instance_id = 审批单 id

⚠️ 样例文件不存在（``samples/`` 里目前只有第 1 份）或库中还没有对应合同时，
**该审批单就不绑定**，如实打印"未绑定"。绑定会在下次运行时补上 —— 这是刻意的：
seed 不为了凑出一个绑定而去创建假合同。

⚠️ **只写 ``contract.approval_instance_id``（合同侧），不写
``approval_instance.contract_id``（审批单侧）**：后者是带 FK 的列，写上之后
"删除合同"就会被外键拦住（``tests/integration/test_golden_sample_e2e.py``
的 ``_purge`` 正是删合同）。回写链路需要的是**合同侧**（§15 与架构裁决都点名
``contract.approval_instance_id``）。将来若 Mock 页面要从审批单反查合同，
由那一步一并决定删除顺序。

幂等性
------
以 ``approval_instance.instance_no`` 为自然键（⚠️ 该列**没有** UNIQUE，见 P3 裁决 O4，
因此靠本脚本的"先查后写"保证不重复；并发跑两次不在本脚本的场景内）：

* 首次执行 → 插入；
* 重复执行 → **不产生新行**，内容对齐到本脚本的定义；
* **不收敛删除**：与 ``seed_rules.py`` 不同，本脚本**不删**它没定义的审批单 ——
  审批单将来来自外部系统（待办拉取），不属于本脚本"权威拥有"的数据。

用法::

    cd backend
    uv run python scripts/seed_mock_approval.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

# 允许 `python scripts/seed_mock_approval.py` 直接跑（与 seed_rules.py 同一手法）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import get_logger
from app.db.models.approval import ApprovalInstance
from app.db.models.contract import Contract, ContractFile
from app.db.session import dispose_engine, session_scope
from app.utils.hash_utils import sha256_file

logger = get_logger(__name__)

#: 样例合同文件所在目录（仓库根 ``samples/``）
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"

#: Mock 审批单状态。**§7.2 没有定义取值**（"取值由 P14 定义，P3 不猜"），
#: 因此这里只用一个最中性的字面量，含义是"待处理" —— 与 ``TaskStatus.PENDING``
#: 同词，但**不是**同一套枚举（审批单状态属于 Mock 域）。
_INSTANCE_STATUS_PENDING = "PENDING"


@dataclass(frozen=True, slots=True)
class SeedInstance:
    """一张待灌入的 Mock 审批单（§14.4）。"""

    instance_no: str
    title: str
    applicant: str
    dept: str
    amount: Decimal | None
    #: 对应 ``samples/`` 里的样例文件名；文件不存在时这张审批单不绑定合同
    sample_file: str


#: §14.4 点名的 4 张审批单。``instance_no`` 是**稳定标识** —— 幂等靠它，
#: 不要因为改了标题就换号（换了等于新建一张审批单）。
SEED_INSTANCES: tuple[SeedInstance, ...] = (
    SeedInstance(
        instance_no="MOCK-AP-001",
        title="软件采购合同",
        applicant="张伟",
        dept="采购部",
        amount=Decimal("580000.00"),
        # ★ 端到端主演示场景：这一份就是 samples/ 里的黄金样例
        sample_file="采购合同-风险版.docx",
    ),
    SeedInstance(
        instance_no="MOCK-AP-002",
        title="大额销售合同",
        applicant="李娜",
        dept="销售部",
        amount=Decimal("3200000.00"),
        sample_file="销售合同-大额.docx",
    ),
    SeedInstance(
        instance_no="MOCK-AP-003",
        title="劳动合同",
        applicant="王强",
        dept="人力资源部",
        # 劳动合同不适用"合同金额"这个语义，如实留空而不是编一个数
        amount=None,
        sample_file="劳动合同.docx",
    ),
    SeedInstance(
        instance_no="MOCK-AP-004",
        title="扫描件采购合同",
        applicant="陈静",
        dept="采购部",
        amount=Decimal("150000.00"),
        sample_file="采购合同-扫描件.pdf",
    ),
)


async def seed_mock_approval() -> dict[str, object]:
    """执行 seed。整个过程在**一个短事务**内完成。"""
    async with session_scope() as session:
        created = updated = 0
        bound: list[tuple[str, int]] = []
        unbound: list[tuple[str, str]] = []

        for spec in SEED_INSTANCES:
            instance, is_created = await _upsert_instance(session, spec)
            if is_created:
                created += 1
            else:
                updated += 1

            contract = await _find_contract_of_sample(session, spec.sample_file)
            if contract is None:
                unbound.append((spec.instance_no, spec.sample_file))
                continue
            if await _bind(session, contract, instance):
                bound.append((spec.instance_no, contract.id))

        await session.flush()

        return {
            "created": created,
            "updated": updated,
            "bound": bound,
            "unbound": unbound,
        }


async def _upsert_instance(session, spec: SeedInstance) -> tuple[ApprovalInstance, bool]:
    """按 ``instance_no`` 找审批单；没有就建，有就对齐内容。

    ⚠️ ``instance_no`` 没有 UNIQUE（P3 裁决 O4：唯一性语义未定义），因此这里
    只能"先查后写"。单进程 seed 脚本足够；并发创建会重复，这是已知边界。
    """
    existing = (
        await session.execute(
            select(ApprovalInstance).where(ApprovalInstance.instance_no == spec.instance_no)
        )
    ).scalar_one_or_none()

    if existing is None:
        instance = ApprovalInstance(
            instance_no=spec.instance_no,
            title=spec.title,
            applicant=spec.applicant,
            dept=spec.dept,
            amount=spec.amount,
            status=_INSTANCE_STATUS_PENDING,
            # ⚠️ 刻意不填 contract_id：见模块 docstring（带 FK，会让合同删不掉）
            contract_id=None,
        )
        session.add(instance)
        await session.flush()
        return instance, True

    existing.title = spec.title
    existing.applicant = spec.applicant
    existing.dept = spec.dept
    existing.amount = spec.amount
    return existing, False


async def _find_contract_of_sample(session, sample_file: str) -> Contract | None:
    """由**样例文件的内容**定位合同（不假定任何 ID）。

    ``contract_file.sha256`` 全局 UNIQUE ⇒ 同一份文件至多属于一个合同，
    因此这条规则是确定的、可复现的。
    """
    path = SAMPLES_DIR / sample_file
    if not path.is_file():
        return None

    digest = await asyncio.to_thread(sha256_file, path)
    contract_id = await session.scalar(select(ContractFile.contract_id).where(ContractFile.sha256 == digest))
    if contract_id is None:
        return None
    return await session.scalar(select(Contract).where(Contract.id == contract_id))


async def _bind(session, contract: Contract, instance: ApprovalInstance) -> bool:
    """把合同指向这张审批单。**只在合同当前没有绑定、或已绑定到同一张时**执行。

    ⚠️ 合同已经绑到**另一张**审批单时不抢：静默改写会让那次绑定凭空消失，
    而 seed 不是绑定关系的权威 —— 它只是把"还没绑的"补上。
    """
    if contract.approval_instance_id == instance.id:
        return False
    if contract.approval_instance_id is not None:
        logger.warning(
            "合同已绑定到其它审批单，seed 不覆盖",
            extra={
                "contract_id": contract.id,
                "existing_instance_id": contract.approval_instance_id,
                "seed_instance_id": instance.id,
            },
        )
        return False

    contract.approval_instance_id = instance.id
    return True


async def _verify() -> list[tuple[str, str | None]]:
    """回读一次：每张审批单当前绑定到哪个合同。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ApprovalInstance.instance_no, Contract.contract_no)
                .outerjoin(Contract, Contract.approval_instance_id == ApprovalInstance.id)
                .where(ApprovalInstance.instance_no.in_([spec.instance_no for spec in SEED_INSTANCES]))
                .order_by(ApprovalInstance.instance_no)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]


async def main() -> int:
    result = await seed_mock_approval()
    rows = await _verify()

    print("[INFO] instances created  :", result["created"])
    print("[INFO] instances updated  :", result["updated"])
    for instance_no, contract_id in result["bound"]:  # type: ignore[union-attr]
        print(f"[ OK ] bound              : {instance_no} -> contract #{contract_id}")
    for instance_no, sample_file in result["unbound"]:  # type: ignore[union-attr]
        print(f"[SKIP] not bound          : {instance_no}（样例 {sample_file} 在 samples/ 或库中不存在）")

    print("[INFO] instances in db    :")
    for instance_no, contract_no in rows:
        print(f"         - {instance_no:<14} contract={contract_no or '（未绑定）'}")

    if len(rows) != len(SEED_INSTANCES):
        print(f"[FAIL] 库中审批单数 {len(rows)} 与 seed 定义数 {len(SEED_INSTANCES)} 不一致")
        await dispose_engine()
        return 1

    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
