"""规则读取接口（P8-0）。

本模块**只做传输层**：解析查询参数、调 Service、返回响应。
不做任何规则求值、不判断风险 —— 那是 Agent 侧求值引擎的事。

⚠️ 路径命名说明
--------------
路径是 ``/rule-sets``（复数，与架构文档 §8 的规则管理接口同名），
但当前**只提供"取当前启用的那一套"这一个读语义**，返回体是
``{contract_type, rule_set, rules}`` 的**单个**规则集快照，不是列表分页。

这样做是为了让"当前启用"的判定只发生在 Backend 一处（见
``services/rule_catalog.py`` 的选取口径说明）—— 如果返回列表让调用方自己挑，
选取规则就会在两个服务里各存一份，容易漂移。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.constants import ContractType
from app.core.logging import get_logger
from app.schemas.rule import EffectiveRuleSetResponse
from app.services.rule_catalog import get_effective_rule_set

logger = get_logger(__name__)

router = APIRouter(tags=["rules"])


@router.get(
    "/rule-sets",
    response_model=EffectiveRuleSetResponse,
    summary="按合同类型读取当前启用的规则集及其启用规则",
    responses={
        200: {
            "description": "查询成功。该合同类型下没有启用的规则集时返回 "
            "rule_set=null 且 rules=[] —— 这不是错误"
        },
        422: {"description": "contract_type 不是合法的合同类型"},
    },
)
async def read_rule_sets(
    contract_type: Annotated[
        ContractType,
        Query(description="合同类型，取值见 constants.ContractType"),
    ],
) -> EffectiveRuleSetResponse:
    """返回该合同类型下**当前启用**的规则集与它的启用规则。

    只读：不创建、不修改、不删除任何规则数据，也不执行规则。
    """
    return await get_effective_rule_set(contract_type.value)


__all__ = ["read_rule_sets", "router"]
