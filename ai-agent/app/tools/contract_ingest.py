"""Agent Tool：把一份合同文件提交给 Backend 完成接入。

Tool 与 BackendClient 的分工
--------------------------
====================  ==========================================================
``BackendClient``     **HTTP 通信**：拼 multipart、发请求、把状态码与响应体
                      翻译成 ``UploadOutcome``。它只知道"有个接口"，不知道"为什么调"
``ContractIngestTool`` **业务动作**：把"提交合同文件进行接入"表达成一个有明确
                      输入/输出契约的动作，并从 Backend 的响应里取出 Agent
                      真正需要的业务字段
====================  ==========================================================

为什么**不**包装成 LangChain 的 ``@tool``
---------------------------------------
``@tool`` 是为**模型决定何时调用**而设计的：它把函数签名渲染成 JSON Schema 交给
LLM，再解析模型给出的调用参数 —— 这套机制的价值全在"让模型自己选工具"。

而这里的调用时机是**确定性的**：工作流的第一步永远是"提交合同文件"，没有任何
需要模型判断的地方。包成 ``@tool`` 只会白白多出一层用不上的 schema 渲染与参数解析。
等 P9/P10 真的出现"由模型决定调哪个工具"的场景时再包装，那时它才有意义。
**现在保持普通的 Python 抽象。**

输入 / 输出契约
---------------
* :class:`ContractIngestRequest` —— 一次接入所需的全部信息，**全是业务字段**，
  不含任何 HTTP 概念（URL / 超时 / 重试 / 状态码）
* :class:`ContractIngestResult` —— 接入结果。失败时 ``ok=False``，
  ``error_code`` / ``error_message`` 有值

⚠️ **Tool 不做业务判断**
------------------------
它不判断文件类型是否支持、不判断返回的字段是否够用。那两件事分别属于：

* **Backend** —— 文件校验（扩展名 / MIME / 魔数 / 大小）
* **``validate_file`` 节点** —— Workflow Gate

Tool 只负责"把动作做掉，把结果如实带回来"。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from app.tools.backend_client import BackendClient


@dataclass(frozen=True, slots=True)
class ContractIngestRequest:
    """一次合同接入所需的输入。"""

    file_path: Path
    filename: str
    contract_no: str
    title: str
    contract_type: str
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class ContractIngestResult:
    """合同接入的输出契约。

    字段与 Backend 的 ``ContractIngestResponse`` 中 Agent 真正需要的部分一一对应 ——
    **不是**那个响应的副本：合同名称、金额、日期等前端可以直接找 Backend 要的数据
    不在这里。

    ``ok=False`` 时所有业务字段为 ``None``，原因在 ``error_code`` / ``error_message``。
    """

    ok: bool

    # ---- 来自 Backend 的标识 ----
    contract_id: int | None = None
    file_id: int | None = None
    review_task_id: int | None = None
    sha256: str | None = None
    file_type: str | None = None
    file_size: int | None = None

    # ---- 两层幂等的信号 ----
    reused: bool | None = None  # 文件层：Backend 复用了已有 ContractFile
    task_reused: bool | None = None  # 任务层：Backend 复用了已有 ReviewTask

    # ---- 失败信息 ----
    error_code: str | None = None
    error_message: str | None = None

    def business_fields(self) -> dict[str, object]:
        """返回 Backend **实际给出**的业务字段（值为 ``None`` 的丢弃）。

        供调用方合并进自己的工作状态。字段清单只在本模块维护一处 ——
        调用方不需要（也不应该）自己再抄一遍字段名。
        """
        skip = {"ok", "error_code", "error_message"}
        return {
            f.name: value
            for f in fields(self)
            if f.name not in skip and (value := getattr(self, f.name)) is not None
        }


# --------------------------------------------------------------------------- #
# 响应字段的防御性取值
#
# Backend 是本项目的另一个服务，它的响应理论上不会错；但"上游返回了意外类型"
# 这类问题一旦发生，会在几个节点之后才以 KeyError/TypeError 的形式炸出来，
# 排查成本远高于在这里挡一下。取不到就返回 None，交给 validate_file 判定。
# --------------------------------------------------------------------------- #
def _as_int(value: object) -> int | None:
    # bool 是 int 的子类，必须排除，否则 True 会被当成合法 ID
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


class ContractIngestTool:
    """Agent 的「合同接入」业务动作。

    :param backend: HTTP 通信层。Tool 只依赖它的抽象行为，不关心它怎么发请求 ——
        因此测试里换一个挂 ``httpx.MockTransport`` 的实例就能跑。
    """

    def __init__(self, backend: BackendClient) -> None:
        self._backend = backend

    async def run(self, request: ContractIngestRequest) -> ContractIngestResult:
        """提交合同文件给 Backend 接入。

        本方法**不抛业务异常**：所有失败都翻译成 ``ok=False`` 的结果，
        由调用它的节点写进 State，再交给 Conditional Edge 分流。
        """
        outcome = await self._backend.upload_contract(
            file_path=request.file_path,
            filename=request.filename,
            content_type=request.content_type,
            contract_no=request.contract_no,
            title=request.title,
            contract_type=request.contract_type,
        )

        if not outcome.ok or outcome.payload is None:
            return ContractIngestResult(
                ok=False,
                error_code=outcome.error_code,
                error_message=outcome.error_message,
            )

        payload = outcome.payload
        return ContractIngestResult(
            ok=True,
            contract_id=_as_int(payload.get("contract_id")),
            file_id=_as_int(payload.get("file_id")),
            review_task_id=_as_int(payload.get("review_task_id")),
            sha256=_as_str(payload.get("sha256")),
            file_type=_as_str(payload.get("file_type")),
            file_size=_as_int(payload.get("file_size")),
            reused=_as_bool(payload.get("reused")),
            task_reused=_as_bool(payload.get("task_reused")),
        )


__all__ = ["ContractIngestRequest", "ContractIngestResult", "ContractIngestTool"]
