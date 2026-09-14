"""Agent Tools。

分两层：

* ``backend_client`` —— HTTP 基础通信（不表达业务含义）
* ``contract_ingest`` —— Agent 的业务动作（有明确的输入/输出契约）

当前只有"合同接入"这一个动作，因为它**已经**被 Graph 用到。
后续阶段的 Tool 在真正需要时再加，不提前铺开。
"""

from app.tools.backend_client import BackendClient, BackendRequestError, UploadOutcome
from app.tools.contract_ingest import ContractIngestRequest, ContractIngestResult, ContractIngestTool

__all__ = [
    "BackendClient",
    "BackendRequestError",
    "ContractIngestRequest",
    "ContractIngestResult",
    "ContractIngestTool",
    "UploadOutcome",
]
