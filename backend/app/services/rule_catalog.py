"""规则读取（P8-0）。

职责
----
::

    DB  →  只读查询  →  EffectiveRuleSetResponse

**Backend 只负责把规则取出来。** 这里没有求值器、不判断风险、不认识条款 ——
规则怎么用是 Agent 侧 `app/rules/` 的事（架构文档 §17.4：规则数据归 Backend、
求值引擎归 Agent）。

"当前启用"的选取规则
------------------
与 ``contract_ingest._active_rule_set_version`` **保持同一口径**：
该合同类型下 ``is_active`` 的规则集里取 **id 最大的一个**。

⚠️ 这一点必须一致 —— ``review_task.idempotency_key`` 里记的 ``rule_set_version``
用的就是那个函数的选取结果。如果这里选出另一套规则集，Agent 评估用的规则
就会和任务幂等键里记录的版本对不上，同一份任务在不同时间可能被算出不同结论。
改动任一侧时**两侧必须一起改**。

为什么直接返回 Pydantic Schema
----------------------------
``services/contract_ingest.py`` 返回的是自己的 dataclass（``IngestResult``），
因为那是**领域结果**、有需要保护的语义。这里是一个**纯读模型** ——
没有领域不变量、也没有跨层翻译要做，再定义一套 dataclass 只是多一次无意义的
字段搬运。字段口径由 ``schemas/rule.py`` 一处定义。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.rule import ReviewRule, ReviewRuleSet
from app.db.session import session_scope
from app.schemas.rule import EffectiveRuleSetResponse, RuleItem, RuleSetSummary

logger = get_logger(__name__)


async def get_effective_rule_set(contract_type: str) -> EffectiveRuleSetResponse:
    """读取该合同类型下当前启用的规则集及其启用规则。

    没有启用的规则集时返回 ``rule_set=None`` + 空 ``rules`` ——
    **不是错误**（架构裁决：没有规则集不能阻止上传）。
    """
    async with session_scope() as session:
        rule_set = await _find_effective_rule_set(session, contract_type)
        if rule_set is None:
            logger.info("该合同类型没有启用的规则集", extra={"contract_type": contract_type})
            return EffectiveRuleSetResponse(contract_type=contract_type, rule_set=None, rules=[])

        rules = await _find_active_rules(session, rule_set.id)

        logger.info(
            "读取到启用规则集",
            extra={
                "contract_type": contract_type,
                "rule_set_id": rule_set.id,
                "rule_set_version": rule_set.version,
                "rule_count": len(rules),
            },
        )
        # 在 session 内组装：ORM 实例一旦离开会话就不该再被访问
        return EffectiveRuleSetResponse(
            contract_type=contract_type,
            rule_set=RuleSetSummary(
                id=rule_set.id,
                name=rule_set.name,
                contract_type=rule_set.contract_type,
                version=rule_set.version,
                description=rule_set.description,
            ),
            rules=[
                RuleItem(
                    id=rule.id,
                    rule_code=rule.rule_code,
                    rule_name=rule.rule_name,
                    dimension=rule.dimension,
                    description=rule.description,
                    rule_type=rule.rule_type,
                    expression=rule.expression,
                    target_clause_types=rule.target_clause_types,
                    severity=rule.severity,
                    legal_basis=rule.legal_basis,
                    suggestion_template=rule.suggestion_template,
                    sort_order=rule.sort_order,
                )
                for rule in rules
            ],
        )


# --------------------------------------------------------------------------- #
async def _find_effective_rule_set(session: AsyncSession, contract_type: str) -> ReviewRuleSet | None:
    """该合同类型下 id 最大的启用规则集。

    ⚠️ 与 ``contract_ingest._active_rule_set_version`` 的选取口径**必须一致** ——
    见模块 docstring。
    """
    stmt = (
        select(ReviewRuleSet)
        .where(
            ReviewRuleSet.contract_type == contract_type,
            ReviewRuleSet.is_active.is_(True),
        )
        .order_by(ReviewRuleSet.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _find_active_rules(session: AsyncSession, rule_set_id: int) -> list[ReviewRule]:
    """规则集下的启用规则，按 ``sort_order`` 升序。

    同 ``sort_order`` 时用 ``id`` 兜底 —— 保证顺序**确定**，不依赖数据库的返回次序。
    """
    stmt = (
        select(ReviewRule)
        .where(
            ReviewRule.rule_set_id == rule_set_id,
            ReviewRule.is_active.is_(True),
        )
        .order_by(ReviewRule.sort_order.asc(), ReviewRule.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = ["get_effective_rule_set"]
