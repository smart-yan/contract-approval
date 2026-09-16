# 合同审批审查系统 —— 项目终态报告

> **基线 commit**：`7846ed3a3bd52cb8caf847b38260be8436cbadbb`  
> **branch**：`main`  
> **状态**：P14 / P15 / P16 已整体 commit + push；P17 当前处于最终文档冻结阶段。

---

## 1. 项目定位

合同审批审查系统是一个用于学习 / 作品集 / 面试展示的 AI Agent 项目。目标是掌握：

* Python 后端 + FastAPI + SQLAlchemy
* AI Agent 服务 + LangGraph + LangChain Core
* LLM 工程化（Provider 抽象、结构化输出、Prompt 版本管理）
* 合同文档解析（DOCX / 全局字符偏移坐标系）
* 规则引擎 + LLM 双通道审查
* 风险统一模型 / 合并 / 持久化
* Workbench（前端双栏 + 风险卡片 + 原文高亮定位）
* 人工复核 / 审批系统写回 / 幂等与异常恢复

**不是生产系统**。架构上**不引入** Redis / Celery / MQ / K8s / JWT / RBAC 等生产级基础设施（见 §12）。

---

## 2. 当前状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1 ~ P13 | 架构设计 / 数据库建模 / 黄金链路 / 报告导出 / 人工复核 | ✅ COMPLETE / FROZEN |
| P14 | 异步执行（Agent 202 + `task_id` + 进程内后台图；任务阻塞上报） | ✅ COMPLETE / FROZEN |
| P15 | 审批写回（`POST /api/v1/review-tasks/{task_id}/writeback`；写回状态机 + 幂等键 + Mock Approval 4 张审批单） | ✅ COMPLETE / FROZEN |
| P16-1 | 异常 / 错误 / 重试 / 集成 Demo 现状侦察 | ✅ COMPLETE |
| P16-2 | Frontend Workbench 审批回写入口 | ✅ COMPLETE / FROZEN |
| P16-3 | 异常路径演示工具与文档（**0** 产品代码修改）| ✅ COMPLETE / FROZEN |
| P16-4 | 文档一致性同步 | ✅ COMPLETE / FROZEN |
| P17-1 | 最终项目只读侦察 | ✅ COMPLETE / FROZEN |
| P17-2 | 最终文档冻结 | ⏳ 当前实施中 |

---

## 3. 整体架构

```text
┌─────────────────────────────────────────────────────────┐
│  Frontend    Vue3 + Vite + Element Plus + Pinia + Axios   │
│              127.0.0.1:5173                               │
│   - 合同列表 / Workbench（双栏 + 风险卡片）              │
│   - 人工复核（确认 / 驳回 / 改等级）                      │
│   - 审批回写触发                                          │
└────────────────────┬───────────────��────────────────────┘
                     │ ① 上传 / 查询 / 审核 / 写回
                     ▼
┌─────────────────────────────────────────────────────────┐
│  AI Agent    FastAPI + LangGraph + LangChain Core        │
│              127.0.0.1:8001                               │
│   - 文档解析（DOCX）                                      │
│   - 条款识别 / 元数据 / 关键词                           │
│   - 规则审查 + LLM 审查                                  │
│   - 风险合并 / 报告数据生成                               │
│   - BackendClient（唯一的 Backend 数据出入口）          │
└────────────────────┬────────────────────────────────────┘
                     │ ② httpx → Backend 领域 API
                     ▼
┌─────────────────────────────────────────────────────────┐
│  Backend    FastAPI + SQLAlchemy 2.x (async)             │
│              127.0.0.1:8000                               │
│   - 合同 / 附件 / 审查任务 / 风险 / 文档块 / 条款 / 元数据│   - 人工复核 / 报告导出                                  │
│   - 任务状态机 + task_event + 心跳自愈                  │
│   - 审批回写（写回状态机 + 幂等键 + ApprovalClient 集成）│
└────────────────────┬────────────────────────────────────┘
                     ▼
                 MySQL 8.4
```

**职责依赖单向**：Frontend → Agent → Backend → MySQL。Backend 从不反向调用 Agent。

---

## 4. 三服务职责边界

### Frontend

只负责：

* UI 展示 / 用户交互
* API 调用（axios 拦截器 + 错误码归一化）
* Workbench（双栏 + 风险卡片 + 原文高亮定位）
* 人工复核（PATCH `/risks/{rid}`）
* 审批回写触发（POST `/writeback`）
* 写回结果展示（SUCCESS / FAILED / 409 `WRITEBACK_ALREADY_SUCCESS`）

**不能出现**：SQLAlchemy / MySQL / DOCX 解析 / LangGraph / LangChain / LLM 调用。

### Agent

负责：

* LangGraph 编排
* 文档解析（DOCX 全局字符偏移坐标系）
* 条款识别 / 元数据提取 / 关键词提取
* 规则求值引擎
* LLM Provider 抽象 + Prompt 版本管理
* 风险合并 / 修改建议
* BackendClient（唯一的 Backend 数据出入口）

**不能出现**：SQLAlchemy / aiomysql / MySQL（由 `pyproject.toml` 依赖声明强制）。

### Backend

负责：

* 持久化（Contract / ContractFile / ReviewTask / RiskItem / DocumentBlock / Clause / Metadata）
* 任务状态机 + `task_event` 审计 + 心跳自愈
* 文件上传 / SHA-256 / 幂等（`contract_file.sha256` UNIQUE）
* 人工复核 / 报告导出（基于已持久化结果实时生成 Markdown）
* 任务阻塞上报（`POST /review-tasks/{task_id}/block`）
* 审批回写（`POST /review-tasks/{task_id}/writeback` + `ApprovalClient` 防腐层 + Mock Approval）

**不能出现**：LangGraph / LangChain / LLM 调用 / Prompt 决策。

---

## 5. Golden Path（端到端 21 步）

```text
Frontend DOCX upload
        ↓
Agent POST /api/agent/review (202 + task_id)
        ↓
Backend pre-upload / task creation
        ↓
Background Graph
        ↓
DOCX Parse
        ↓
Clause Identification
        ↓
Metadata
        ↓
Keyword
        ↓
Rule Review
        ↓
LLM Review
        ↓
Risk Merge
        ↓
Document Persistence (current_stage = CLAUSED)
        ↓
Risk Persistence (current_stage = REVIEWED)
        ↓
Frontend Polling
        ↓
Workbench
        ↓
Human Review
        ↓
POST /api/v1/review-tasks/{task_id}/writeback
        ↓
Backend 解析 ReviewTask → Contract → contract.approval_instance_id
        ↓
ApprovalClient
        ↓
MockApprovalClient.post_comment (instance_id, content_md, idempotency_key)
        ↓
approval_comment (source = CONTRACT_REVIEW_SYSTEM)
        ↓
WritebackRecord SUCCESS
```

---

## 6. 核心技术链路

### 6.1 文档解析

`DocxParser` 遍历 body 元素（段落 + 表格），按 XML 顺序产出 `document_block`；每个 block 含 `char_start_global / char_end_global`（全文档统一坐标系）。前端 `docx-preview` 按 `paragraph_index` 渲染。

### 6.2 Clause / Metadata

规则优先 + LLM 兜底：编号模式（第[一二三…]条 / `^\d+\.\d*` / 第 X 章）命中 ≥ 3 条视为结构化合同；失败则 LLM 切分（结构化 JSON 输出，schema 强校验）；再失败则降级为"一段一 clause"。

### 6.3 Rule + LLM 双通道审查

* **Rule Engine**：5 种求值器（KEYWORD / REGEX / EXISTS / MISSING / THRESHOLD）。确定性 / 可解释 / 零成本 / 可审计。
* **LLM Review**：按 `clause_type` 分批（≤ 8 条款 / 批，≤ 6000 字符）；`asyncio.Semaphore(4)` 限流；输出 Pydantic 严格校验。
* LLM **只**输出 `quote`，坐标由 **Agent** 在原文中反查回填（杜绝幻觉偏移）。

### 6.4 Risk Merge

合并键 = `(clause_id, dimension, 归一化 risk_title)`。同时命中 → `source=RULE+LLM`，level 取二者较高，reason 合并。

### 6.5 Risk Persistence

`backend/app/services/risk_persistence.py:246` 在整批风险写入后将 `current_stage` 推到 `REVIEWED`；门禁要求 `CLAUSED` 状态；幂等保护（`UNIQUE(task_id, clause_id, dimension, risk_title)`）。

### 6.6 Workbench

`GET /api/v1/review-tasks/{task_id}/workbench` 一次返回渲染所需的全部数据（task / contract / file / blocks / clauses / metadata / risks）。风险卡片 → 原文段落定位（P11-7）。

### 6.7 Human Review

`PATCH /api/v1/review-tasks/{task_id}/risks/{risk_id}` 修改 `review_status`（PENDING / CONFIRMED / REJECTED / MODIFIED）+ `review_comment` + `risk_level`（仅 MODIFIED）。**不**推进 `current_stage`，**不**重算综合等级。

### 6.8 Approval Writeback

* `POST /api/v1/review-tasks/{task_id}/writeback`（**唯一**端点，**无** body）
* `approval_instance_id` 由 Backend 沿 `ReviewTask → Contract → contract.approval_instance_id` 解析（前端**不传**）
* 幂等键：`sha256(task_id + content_hash)`
* `UNIQUE(instance_id, idempotency_key)` 双层去重
* 重试由**同一** endpoint 通过 `FAILED → WRITING` 状态机完成（**不**存在 `/retry` 或 `/reconcile`）

---

## 7. 幂等与异常恢复

| 场景 | 当前行为 | 恢复路径 |
|---|---|---|
| 同一文件 SHA-256 重复上传 | 返回 200 + `reused=true` + 同 `task_id` | 自动 |
| 同一任务风险重复持久化 | 409 `TASK_ALREADY_PERSISTED` | 视为已成功 |
| 同一文档层重复持久化 | 409 `DOCUMENT_ALREADY_PERSISTED` | 视为已成功 |
| Agent graph 抛异常 | `_report_blocked(AGENT_GRAPH_EXECUTION_FAILED)` | 人工 unblock 后重试 |
| Agent graph 取消（shutdown） | 同上报 + re-raise | 同上 |
| 写回 transport failure | `WritebackRecord.status = FAILED` + 错误原因 | 再 POST 同 `task_id`（FAILED → WRITING） |
| 写回业务拒绝 | `FAILED` + `AppError` re-raise | 人工修复后重试 |
| 写回 SUCCESS 重复调用 | 409 `WRITEBACK_ALREADY_SUCCESS` | 保护终态 |
| 写回响应丢失（外部已落库） | `find_comment` 命中 → `success` + `posted=false` | 自带恢复 |
| 任务 stage 非 REVIEWED | 409 `WRITEBACK_NOT_READY` | 等任务完成 |
| `approval_instance` 缺失 | 409 `WRITEBACK_APPROVAL_NOT_READY` | 关联审批单 |
| `approval_instance` 行不存在 | 404 `NOT_FOUND` | 重建关联 |
| LLM 不可用 | `LLMUnavailableError` → rules-only + warning | 任务继续 |

---

## 8. API 总览

### Backend（`/api/v1`，11 个端点）

| Method | Path | 用途 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/v1/contracts` | 合同列表 |
| POST | `/api/v1/contracts` | 合同上传 |
| GET | `/api/v1/rule-sets` | 当前生效规则集 |
| POST | `/api/v1/review-tasks/{task_id}/document` | 文档层持久化 |
| POST | `/api/v1/review-tasks/{task_id}/risks` | 风险整批写入 |
| PATCH | `/api/v1/review-tasks/{task_id}/risks/{risk_id}` | 人工复核 |
| GET | `/api/v1/review-tasks/{task_id}/report/export` | 报告下载（Markdown）|
| GET | `/api/v1/review-tasks/{task_id}/workbench` | 工作台查询 |
| POST | `/api/v1/review-tasks/{task_id}/block` | 任务阻塞上报 |
| POST | `/api/v1/review-tasks/{task_id}/writeback` | 审批回写 |

### Agent（`/api/agent`，1 个业务端点 + /health）

| Method | Path | 用途 |
|---|---|---|
| POST | `/api/agent/review` | 受理审查（202 + `task_id`）|

### Frontend API 调用模块（`frontend/src/api/`，6 个）

| 文件 | 主要函数 |
|---|---|
| `request.ts` | Axios 基础封装（拦截器 + ApiError 归一化）|
| `agent-review.ts` | `startContractReview` |
| `contracts.ts` | `getContracts` |
| `risk-review.ts` | `reviewRiskItem` |
| `system.ts` | `fetchHealth` |
| `workbench.ts` | `getReviewTaskWorkbench` |
| `writeback.ts` | `postWriteback`（P16-2 新增）|

---

## 9. 测试与验证

**实际测试基线**（commit `7846ed3` 时点）：

| 类别 | 数量 |
|---|---|
| Backend unit | 558 passed |
| Backend integration | 344 passed |
| **Backend 合计** | **902 passed** |
| Agent unit | 906 passed |
| Agent integration | 150 passed（其中 1 skipped）|
| **Agent 合计** | **1056 passed, 1 skipped**（Golden Path integration tests = 15，作为子集说明，**不**重复计入总数）|
| Frontend | 181 passed |
| **总计** | **2139 passed, 1 skipped** |

代码规模（实际行数）：

* Backend product code：约 10,219 行
* Backend test code：约 13,354 行
* Agent product code：约 8,573 行
* Agent test code：约 14,584 行
* Frontend src code：约 3,189 行
* Frontend test code：约 3,511 行

测试代码规模约为产品代码的 **1.89 倍**。

**质量门**：

* `vue-tsc --build`：clean
* `ruff check backend/`：All checks passed
* `git diff --check`：clean

---

## 10. 已知 TECH DEBT

以下为 P16-1 已记录的已知边界，**不在 P17 修复范围**：

| # | 项目 | 位置 |
|---|---|---|
| 1 | `_report_blocked` 失败可能造成 orphan | `ai-agent/app/api/review.py:235-266` |
| 2 | Agent 进程重启导致 background task orphan | `ai-agent/app/background.py:42-48` |
| 3 | Agent error code fallback `INTERNAL_ERROR` 丢失操作信号 | `ai-agent/app/api/review.py:209-212` |
| 4 | Frontend polling 失败后需手动重试（无 backoff）| `frontend/src/composables/useReviewPolling.ts` |
| 5 | Frontend 无 writeback 历史面板 | `frontend/src/views/workbench/WorkbenchView.vue` |
| 6 | 30s axios timeout 与 BackgroundReviews drain 30s 边沿 | `frontend/src/api/request.ts:27` |

---

## 11. 明确不做的能力

以下为项目架构裁决明确**不实现**的能力，**不**作为未来 TODO：

* **JWT / RBAC / 登录**（项目为学习 / 面试项目，单机演示不需要鉴权）
* **真实第三方审批系统**（Mock Approval 即防腐层足够）
* **跨进程任务队列**（Agent 进程内后台图 + P14 状态机 + 心跳自愈足够）
* **OCR / PDF**（黄金链路只做 DOCX，扩展阶段未启用）
* **Redis / Celery / MQ / K8s / 服务注册 / 分布式锁 / 复杂 HA**
* **`writeback history table`**（架构裁决："一份任务一份写回记录"）
* **`/retry` 端点 / `/reconcile` 端点**（重试由同一 endpoint 通过 `FAILED → WRITING` 状态机完成）
* **Agent ��连数据库**（架构红线，由 `pyproject.toml` 依赖声明强制）
* **`interrupt()` / checkpoint 实现人工审核**（人工审核是普通状态流转，不进入 LangGraph）

---

## 12. 架构红线

### Frontend 不能直接访问

* MySQL
* SQLAlchemy
* DOCX 解析器
* LangGraph
* LangChain
* LLM 调用

### Agent 不能直接访问

* SQLAlchemy
* aiomysql
* MySQL

### Backend 不负责

* LangGraph 编排
* LangChain 调用
* LLM 推理
* Prompt 决策

### 依赖方向严格单向

```text
Frontend
   ↓
Agent
   ↓
Backend
   ↓
MySQL
```

**禁止**反向调用。Backend 从不调 Agent；Agent 通过 `httpx → Backend` 单向通信。

---

## 13. 当前 Git 基线

```text
Branch:    main
Commit:    7846ed3a3bd52cb8caf847b38260be8436cbadbb
Title:     feat: complete P15 writeback + P16 error/retry/integration demo
Status:    P16 已整体 commit + push
           P17 当前处于最终文档冻结阶段
```

---

**报告结束**。本报告基于实际代码、测试与文档生成，与 `commit 7846ed3` 一一对应。任何后续修改请保持本文档与代码同步。