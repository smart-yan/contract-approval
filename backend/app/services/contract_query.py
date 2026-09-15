"""合同查询（P11-3）。

职责边界
-------
::

    DB  →  一次 SELECT  →  list[ContractListItem]

与 ``rule_catalog.py`` 同构：**只读、无领域不变量、直接返回 Pydantic schema**。
这里是查询侧，不落库、不改状态、不碰 P10 的任何写入路径。

列表状态从哪来（本模块最要紧的一条口径）
------------------------------------
**不用** ``Contract.status`` / ``Contract.current_task_id`` —— 那两个字段在 P4
建合同时写下后就没人维护（``status`` 恒为建库时的值、``current_task_id`` 恒为
``NULL``），拿它们当状态来源就是在展示一个陈旧的事实。

真正的进度信号是 ``review_task.current_stage``：它由文档层（``UPLOADED`` →
``CLAUSED``）与风险层（``CLAUSED`` → ``REVIEWED``）真实推进。

``latest_task`` 的定义
--------------------
::

    同一个 contract_id 下，id 最大的那条 ReviewTask

一个合同可以有多条任务（换规则集 / 换 prompt 就会新建，见 §6.1「历史任务全留存」），
列表页要的是**最近一次**。没有任务时为 ``None`` —— **不伪造默认任务**。

为什么不用 ORM relationship
--------------------------
``Base`` 下的模型一个 ``relationship()`` 都没有，这是刻意的（避免懒加载在异步
会话里炸、也避免"看着是属性其实是查询"）。本模块沿用这个约定，
用**显式 SQL** 拿数据。
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.constants import TaskStage
from app.core.logging import get_logger
from app.db.models.contract import Contract
from app.db.models.review_task import ReviewTask
from app.db.session import session_scope
from app.schemas.contract import ContractListItem, ReviewTaskSummary

logger = get_logger(__name__)

#: ``current_stage`` → 进度百分比。
#:
#: ⚠️ **数据库里那一列（``review_task.progress``）从来没有被写过**（恒为 0），
#: 因此这里的值是 API 侧**推导**出来的展示口径，不是测量值。
#:
#: 为什么放在这一处、而不是散在 SQL 或 API handler 里：这是一个**读模型投影**，
#: 只服务于展示；留一个明确的定义点，将来要改成真实进度时只改这里。
#: ``test_progress_covers_every_known_stage`` 会断言这张表覆盖了 ``TaskStage``
#: 的**全部**取值 —— 以后新增阶段却忘了给进度，测试会先炸。
_STAGE_PROGRESS: dict[str, int] = {
    TaskStage.UPLOADED.value: 0,  # 已接入，还没解析
    TaskStage.PARSED.value: 30,  # 解析完成
    TaskStage.CLAUSED.value: 60,  # 条款/元数据已落库
    TaskStage.REVIEWED.value: 100,  # 风险已落库，这次审查跑完了
}

#: 阶段不在词表里时的进度。刻意用一个**显眼但不撒谎**的值：
#: 数据里出现了未知阶段说明有人改了枚举却没改这里，此时报 0 会让人以为"还没开始"。
_UNKNOWN_STAGE_PROGRESS = 0


def progress_for_stage(current_stage: str) -> int:
    """阶段 → 进度百分比。未知阶段按 0（并且会被 ``logger`` 记一笔）。"""
    progress = _STAGE_PROGRESS.get(current_stage)
    if progress is None:
        logger.warning("未知的 task stage，进度按 0 处理 | current_stage=%s", current_stage)
        return _UNKNOWN_STAGE_PROGRESS
    return progress


async def list_contracts() -> list[ContractListItem]:
    """列出全部合同（按创建时间倒序），每项带上它的最新审查任务。

    **一次 SQL**，没有 N+1 —— 见 :func:`_list_contracts_stmt` 的说明。

    ``ORDER BY created_at DESC, id DESC``：``created_at`` 可能撞毫秒，
    再拿 ``id`` 兜底才能保证顺序**稳定**（否则两次请求可能给出不同的次序，
    而分页/对比都依赖这个稳定性）。P11-2 的 ``ix_contract_created_at_id``
    正是为这条排序建的。
    """
    async with session_scope() as session:
        rows = (await session.execute(_list_contracts_stmt())).all()

    items = [
        ContractListItem(
            contract_id=contract.id,
            contract_no=contract.contract_no,
            title=contract.title,
            contract_type=contract.contract_type,
            created_at=contract.created_at,
            latest_task=(
                None
                if task is None
                else ReviewTaskSummary(
                    task_id=task.id,
                    status=task.status,
                    current_stage=task.current_stage,
                    progress=progress_for_stage(task.current_stage),
                )
            ),
        )
        for contract, task in rows
    ]

    logger.info("合同列表查询完成 | contracts=%d", len(items))
    return items


def _list_contracts_stmt():
    """列表查询的 SQL 构造（**一次往返**，无 N+1）。

    做法是「greatest-n-per-group」的经典写法：先用一个**关联子查询**取出每个合同的
    最新任务 id，再用它 ``LEFT JOIN`` 回 ``review_task`` 拿整行。

    ::

        SELECT contract.*, review_task.*
        FROM contract
        LEFT JOIN review_task
               ON review_task.id = (SELECT id FROM review_task
                                    WHERE contract_id = contract.id
                                    ORDER BY id DESC LIMIT 1)
        ORDER BY contract.created_at DESC, contract.id DESC

    为什么不是一个循环里逐个合同再查一次：那是 N+1 —— 列表页每次刷新会发
    ``1 + N`` 条 SQL，N 大一点就把连接池和延迟都吃掉。这里子查询在**数据库内部**
    逐合同求值，配合 P11-2 的 ``ix_review_task_contract_id_id(contract_id, id)``
    就是一次索引反向扫描取第一条，并且只走**一次**网络往返。

    ⚠️ **必须是 LEFT JOIN**：没有任务的合同也要出现在列表里（``latest_task=null``），
    INNER JOIN 会把它们整条丢掉 —— 而"上传了但还没发起审查"恰恰是最需要被看到的
    状态之一。
    """
    latest_task_id = (
        select(ReviewTask.id)
        .where(ReviewTask.contract_id == Contract.id)
        .order_by(ReviewTask.id.desc())
        .limit(1)
        .correlate(Contract)
        .scalar_subquery()
    )

    return (
        select(Contract, ReviewTask)
        .outerjoin(ReviewTask, ReviewTask.id == latest_task_id)
        .order_by(Contract.created_at.desc(), Contract.id.desc())
    )


__all__ = ["list_contracts", "progress_for_stage"]
