"""灌入采购合同规则集及其基础规则（架构文档 §17 的 P3 交付项、§11.1）。

职责边界
--------
本脚本**只写基础数据**：一个 ``review_rule_set`` 与 3 条 ``review_rule``。
它**不实现**规则引擎、不执行规则、不做风险检测 —— 那些属于 **P9**。
``expression`` 里的 JSON 只是**数据**，其求值契约由 P9 定义。

为什么只有 3 条规则（而不是 §11.1 列出的全部维度）
------------------------------------------------
只保留 ``expression`` 契约在架构文档中**已有明确示例**的类型：

* ``KEYWORD``   —— §7.2 给出 ``{"keywords": [...], "logic": "ANY"}``
* ``THRESHOLD`` —— §7.2 给出 ``{"field": ..., "op": ..., "value": ...}``

``MISSING``（必备条款缺失）类型 §7.2 **没有给出任何表达式示例**。
按架构裁决，P3 **不自行发明**该契约 —— 对应的两条规则
（付款前置验收、相对方统一社会信用代码）暂不 seed，
待 **P9** 定义 MISSING 的求值契约后再增量补充。

幂等性（upsert + 收敛）
-----------------------
以「规则集 name」和「规则 (rule_set_id, rule_code)」为自然键：

* 首次执行 → 插入；
* 重复执行 → 不产生重复行，内容对齐到本脚本的定义；
* **收敛**：本规则集内、但不在本脚本定义中的规则会被删除 ——
  否则"幂等"只意味着"不重复插入"，却无法让数据库收敛到脚本声明的状态，
  旧版本 seed 留下的行会永久残留。

⚠️ 因此本脚本对本规则集是**权威**的：若在管理界面（P13）改过或新增过
本规则集内的规则，重跑会**覆盖/删除**那些改动。需要保留人工改动时请勿重跑，
或为自定义规则另建规则集。

用法::

    cd backend
    uv run python scripts/seed_rules.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.constants import ClauseType, ContractType, RiskLevel, RuleType
from app.db.models.rule import ReviewRule, ReviewRuleSet
from app.db.session import dispose_engine, session_scope

# --------------------------------------------------------------------------- #
# 规则集定义（§11.1：预置「采购合同审查清单 v1」）
# --------------------------------------------------------------------------- #
RULE_SET_SPEC: dict[str, Any] = {
    "name": "采购合同审查清单 v1",
    "contract_type": ContractType.PURCHASE.value,
    "description": "适用于我方作为采购方的软件/货物采购合同。当前内置知识产权、违约责任、金额支付三个维度的基础规则；"
    "主体资质、付款验收等 MISSING 类规则待 P9 定义表达式契约后增量补充。",
    "version": "v1",
    "is_active": True,
    "created_by": None,  # sys_user 属 P4，暂为空
}

# --------------------------------------------------------------------------- #
# 规则定义（§11.1 的规则维度表）
#
# ⚠️ 入选标准：``expression`` 的契约必须在架构文档中有**明确示例**。
#   · KEYWORD   —— §7.2 给出 {"keywords": [...], "logic": "ANY"}     ✔ 已入选
#   · THRESHOLD —— §7.2 给出 {"field": ..., "op": ..., "value": ...} ✔ 已入选
#   · MISSING   —— §7.2 **没有任何示例**                              ✘ 暂不 seed
#
# 因此「未设置付款前置验收条件」与「相对方统一社会信用代码缺失」两条
# MISSING 规则**不在本列表内**，待 P9 定义契约后增量补充。
# --------------------------------------------------------------------------- #
RULE_SPECS: list[dict[str, Any]] = [
    {
        "rule_code": "IP_OWNER_SUPPLIER_001",
        "rule_name": "知识产权归属相对方",
        "dimension": "知识产权",
        "description": "采购场景下，若约定成果或软件的知识产权归供方所有，我方将无法自由使用与二次开发。",
        "rule_type": RuleType.KEYWORD.value,
        "expression": {
            "keywords": ["知识产权归供方", "知识产权归乙方", "知识产权归供应商", "所有权归乙方"],
            "logic": "ANY",
        },
        "target_clause_types": [ClauseType.IP.value],
        "severity": RiskLevel.HIGH.value,
        "legal_basis": "《民法典》第 843 条：技术合同的内容由当事人约定。"
        "采购场景下若知识产权归属供方，需同步约定我方的使用许可范围，否则影响后续使用与二次开发。",
        "suggestion_template": "建议修改为：本项目产生的软件著作权、源代码及相关技术文档的知识产权归甲方所有；"
        "乙方在交付时须一并交付完整源代码及技术文档，并保证不侵犯第三方知识产权。",
        "is_active": True,
        "sort_order": 10,
    },
    {
        "rule_code": "LIAB_UNLIMITED_001",
        "rule_name": "我方单方承担无限责任",
        "dimension": "违约责任",
        "description": "出现「全部损失」「无限责任」「不设上限」等表述且未限定对等义务时，我方赔偿范围可能远超合同金额。",
        "rule_type": RuleType.KEYWORD.value,
        "expression": {
            "keywords": ["全部损失", "无限责任", "不设上限", "承担一切责任", "赔偿全部"],
            "logic": "ANY",
        },
        "target_clause_types": [ClauseType.LIABILITY.value],
        "severity": RiskLevel.HIGH.value,
        "legal_basis": "《民法典》第 584 条：损失赔偿额应当相当于因违约造成的损失，"
        "但不得超过违约一方订立合同时预见到或者应当预见到的因违约可能造成的损失。",
        "suggestion_template": "建议修改为：任何一方的累计赔偿责任以本合同总金额为上限；"
        "因故意或重大过失造成的损失不受此限。双方赔偿责任对等。",
        "is_active": True,
        "sort_order": 30,
    },
    {
        "rule_code": "PAY_PREPAY_RATIO_001",
        "rule_name": "预付款比例超过 30%",
        "dimension": "金额支付",
        "description": "预付比例过高会显著增加我方资金占用与对方不履约时的追偿风险。",
        "rule_type": RuleType.THRESHOLD.value,
        "expression": {"field": "prepay_ratio", "op": "gt", "value": 0.3},
        "target_clause_types": [ClauseType.AMOUNT_PAYMENT.value],
        "severity": RiskLevel.MEDIUM.value,
        "legal_basis": "企业采购内控通常要求预付款不超过合同总额的 30%，以控制资金风险。",
        "suggestion_template": "建议调整为：预付款比例不超过合同总额的 30%，余款按交付与验收里程碑分期支付。",
        "is_active": True,
        "sort_order": 50,
    },
]


# --------------------------------------------------------------------------- #
# seed 逻辑
# --------------------------------------------------------------------------- #
async def _upsert_rule_set(session) -> tuple[ReviewRuleSet, bool]:
    """按 name 幂等地写入规则集，返回 (实例, 是否新建)。"""
    stmt = select(ReviewRuleSet).where(ReviewRuleSet.name == RULE_SET_SPEC["name"])
    rule_set = (await session.execute(stmt)).scalar_one_or_none()

    if rule_set is None:
        rule_set = ReviewRuleSet(**RULE_SET_SPEC)
        session.add(rule_set)
        await session.flush()  # 取到自增 id，供下面的规则引用
        return rule_set, True

    for key, value in RULE_SET_SPEC.items():
        setattr(rule_set, key, value)
    await session.flush()
    return rule_set, False


async def _upsert_rule(session, rule_set_id: int, spec: dict[str, Any]) -> bool:
    """按 (rule_set_id, rule_code) 幂等地写入规则，返回是否新建。

    这一对正是 ``uq_review_rule_rule_set_id_rule_code`` 唯一约束的列，
    因此"是否已存在"与数据库的约束语义完全一致。
    """
    stmt = select(ReviewRule).where(
        ReviewRule.rule_set_id == rule_set_id,
        ReviewRule.rule_code == spec["rule_code"],
    )
    rule = (await session.execute(stmt)).scalar_one_or_none()

    if rule is None:
        session.add(ReviewRule(rule_set_id=rule_set_id, **spec))
        return True

    for key, value in spec.items():
        setattr(rule, key, value)
    return False


async def _delete_stale_rules(session, rule_set_id: int) -> int:
    """删除本规则集内**不在 RULE_SPECS 中**的规则，返回删除条数。

    这是"幂等"的必要组成部分：只做 upsert 的话，旧版本 seed 留下的行会永久残留，
    数据库无法收敛到脚本声明的状态（例如已从列表移除的 MISSING 规则）。
    """
    declared_codes = {spec["rule_code"] for spec in RULE_SPECS}
    stmt = select(ReviewRule).where(ReviewRule.rule_set_id == rule_set_id)
    existing = list((await session.execute(stmt)).scalars())

    deleted = 0
    for rule in existing:
        if rule.rule_code not in declared_codes:
            await session.delete(rule)
            deleted += 1
    return deleted


async def seed_rules() -> dict[str, int]:
    """执行 seed，返回统计结果。整个过程在**一个短事务**内完成。"""
    async with session_scope() as session:
        rule_set, rule_set_created = await _upsert_rule_set(session)

        created = updated = 0
        for spec in RULE_SPECS:
            if await _upsert_rule(session, rule_set.id, spec):
                created += 1
            else:
                updated += 1

        deleted = await _delete_stale_rules(session, rule_set.id)
        await session.flush()

        return {
            "rule_set_id": rule_set.id,
            "rule_set_created": int(rule_set_created),
            "rules_created": created,
            "rules_updated": updated,
            "rules_deleted": deleted,
        }


async def _verify(rule_set_id: int) -> tuple[int, list[tuple[str, str, str]]]:
    """回读一次，确认库里的实际内容。"""
    async with session_scope() as session:
        stmt = (
            select(ReviewRule.rule_code, ReviewRule.severity, ReviewRule.rule_type)
            .where(ReviewRule.rule_set_id == rule_set_id)
            .order_by(ReviewRule.sort_order)
        )
        rows = list((await session.execute(stmt)).all())
        return len(rows), [(r[0], r[1], r[2]) for r in rows]


async def main() -> int:
    result = await seed_rules()
    count, rows = await _verify(result["rule_set_id"])

    print("[INFO] rule_set_id        :", result["rule_set_id"])
    print("[INFO] rule_set created   :", bool(result["rule_set_created"]))
    print("[INFO] rules created      :", result["rules_created"])
    print("[INFO] rules updated      :", result["rules_updated"])
    print("[INFO] rules deleted      :", result["rules_deleted"], "(本规则集内已不在定义中的残留规则)")
    print("[ OK ] rules in database  :", count)
    for code, severity, rule_type in rows:
        print(f"         - {code:<26} severity={severity:<7} type={rule_type}")

    if count != len(RULE_SPECS):
        print(f"[FAIL] 库中规则数 {count} 与 seed 定义数 {len(RULE_SPECS)} 不一致")
        await dispose_engine()
        return 1

    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
