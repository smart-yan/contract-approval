# 合同审批审查系统（Contract Approval & Review System）

面向企业合同审批场景的智能审查系统：支持多格式合同解析与条款结构化切分，通过**合规规则引擎 + 大语言模型**识别法律与商业风险，提供原文高亮定位、修改建议与报告导出，并支持将审查意见回写至审批系统评论区。

> 架构设计见 [`docs/01-架构设计.md`](docs/01-架构设计.md)。
>
> **本 README 当前只记录已完成并验证过的内容。** 完整的启动、部署与接口文档在 P16 阶段补齐。

---

## 当前阶段进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1 | 架构设计 | ✅ PASS（架构文档已确认） |
| P2-a | 版本控制 / 依赖 / 目录骨架 | ✅ PASS |
| P2-b | core 基座（config / constants / logging / errors） | ✅ PASS |
| P2-c | 数据库基础设施（async engine / session 生命周期 / Alembic / UTC 会话时区） | ✅ PASS |
| P2-d | 并发基座 + FastAPI 入口 + lifespan + `/health` + 异常处理 | ✅ PASS |
| P2-e | 前端基础壳工程（Vue3 + Vite + Element Plus + Router + Pinia + Axios） | ✅ PASS |
| P3 | 后端数据库建模（15 张业务表 + baseline 迁移 + 规则 seed） | ✅ PASS |
| P4 | 合同接入（storage 抽象 / SHA256 / 三层文件校验 / 幂等 / 并发竞争 / 孤儿清理 / 上传 API） | ✅ PASS（328 tests） |
| P5-1 | AI Agent Preflight | ✅ PASS |
| P5-2 | AI Agent 服务外壳（FastAPI + 配置 + 目录骨架 + 健康检查） | ✅ PASS |
| P5-3 | 最小 LangGraph（State + 3 个节点 + Conditional Edge） | ✅ PASS |
| P6 | 文档解析（`DocumentParser` 抽象 + `DocxParser`，段落/表格按 XML 顺序，块与全局偏移） | ✅ PASS |
| P7 | 理解层（条款识别 / 元数据提取 / 关键词提取） | ✅ PASS |
| P8 | 规则审查（规则目录 API + 求值引擎：KEYWORD / REGEX / THRESHOLD） | ✅ PASS |
| P9 | LLM 审查 + 风险合并（Provider + JSON 守卫 + quote 定位 + 统一风险模型 + 合并） | ✅ PASS |
| P9-10 | 风险持久化（整批原子写入 + 阶段门禁 + 行锁） | ✅ PASS |
| P10 | 文档持久化（block / clause / metadata 落库，图收敛为 11 节点） | ✅ PASS |
| P11 | 前端工作台（合同列表 + 工作台双栏 + 风险卡片 → 原文滚动高亮） | ✅ PASS |
| P12 | 报告导出（Markdown 报告 + RFC 5987 中文文件名下载） | ✅ PASS |
| P13 | 人工复核（复核 PATCH API + 工作台复核 UI + 报告复核标注） | ✅ PASS（`P13-1` ~ `P13-4` 已评审冻结；`P13-5` 收尾中） |
| P14 | 异步执行（Agent `POST /api/agent/review` 返回 202 + `task_id`，进程内后台图；Agent ↔ Backend 解耦；任务阻塞上报 `POST .../review-tasks/{task_id}/block`） | ✅ PASS / FROZEN |
| P15 | 审批写回（`POST /api/v1/review-tasks/{task_id}/writeback`；`writeback_record` 状态机 + `idempotency_key` UNIQUE；`approval_comment.idempotency_key` 与 `source` 列扩展；`ApprovalClient` 防腐层 + `MockApprovalClient`；`scripts/seed_mock_approval.py` 4 张审批单；39 个写回集成测试） | ✅ PASS / FROZEN |
| P16-1 | 异常 / 错误 / 重试 / 集成 Demo 现状侦察（只读审计：49 个 `ErrorCode` 矩阵、Retry 矩阵、P14→P15 集成链路、6 个 Demo 场景、测试隔离、Mock Approval 复现性） | ✅ COMPLETE |
| P16-2 | Frontend Workbench 审批回写入口（`frontend/src/api/writeback.ts` + `WorkbenchView.vue`「回写审批意见」操作；SUCCESS / FAILED / 409 `WRITEBACK_ALREADY_SUCCESS` 三态；17 个 frontend writeback 测试） | ✅ PASS / FROZEN |
| P16-3 | 异常路径演示工具与文档（`backend/scripts/demo_writeback_failures.py` 6 个场景 + `docs/16-异常路径演示.md` 索引；**0** 产品代码修改） | ✅ PASS / FROZEN |
| P16-4 | 文档一致性同步（`README.md` + `docs/01-架构设计.md` 与真实代码 / 测试对齐） | ✅ PASS / FROZEN |
| P17-1 | 最终项目只读侦察（Git / API / 边界 / Migration / 测试 / Golden Path / Error-Retry 全方位审计；2139 passed + 1 skipped 验证） | ✅ COMPLETE / FROZEN |
| P17-2 | 最终文档冻结（`README.md` 测试口径统一 + `docs/PROJECT_FINAL.md` 项目终态报告） | ⏳ 当前实施中 |

> **当前状态**：P14 / P15 / P16 已整体 commit + push（commit `7846ed3a3bd52cb8caf847b38260be8436cbadbb`，branch `main`）。P17-1 完成只读侦察；P17-2 当前正在做最终文档冻结。
>
> 尚未实现的业务：OCR（扫描件）、修改建议生成（`risk_suggestion`）、前端文档预览。这些均**不在 P17 范围**。
>
> 全部测试基线（实际运行验证）：
>
> * Backend：**unit 558 passed + integration 344 passed = 902 passed**
> * Agent：**unit 906 passed + integration 150 passed（1 skipped）= 1056 passed, 1 skipped**（其中 Golden Path integration tests = 15，作为子集说明，**不**重复计入 Agent integration 总数）
> * Frontend：**181 passed**
>
> 合计：**2139 passed, 1 skipped**

### 已知技术债（已接受，不在当时阶段内修复）

| # | 技术债 | 说明 |
|---|---|---|
| 1 | **人工改等级会覆盖 AI 原始值** | 人工复核（P13）采用 Scheme A：直接就地改写 `risk_item`，不建复核历史表。因此 `MODIFIED` 之后**无法恢复 AI 原始风险等级**，报告的**风险概览按当前 `risk_item.risk_level` 统计**。已接受，不引入 `original_risk_level` / 历史表 / 迁移 |
| 2 | **`reviewer_id` 恒为 NULL** | 系统尚未引入用户身份与权限体系（JWT / RBAC / `sys_user` 明确不做），因此复核人一列**保持为空**，不伪造任何用户名 |
| 3 | 请求体校验错误未走统一错误体 | FastAPI 默认的 422 响应体是 `{"detail": [...]}`，没有 `code` / `message`，前端拦截器会把它归成 `NETWORK_ERROR`。前端按 `status === 422` 兜底，但拿不到后端那句精确文案 |
| 4 | 路由白名单按**路径**比较 | `tests/unit/test_main_wiring.py` 的守卫抓不到"在已有路径上新增方法"（例如给 `/risks` 加 DELETE） |
| 5 | 部分集成测试夹具的时区 | 8 个历史集成测试文件用裸 `pymysql` 直连，绕过了 `SET time_zone='+00:00'`，其 `NOW(3)` 写入的是本地时间。这些测试目前不做时间断言，故无假阳性（P12/P13 的新夹具已修正） |

---

## 数据库（P3 完成）

MySQL 8.4.8，库 `contract_approval`，字符集 `utf8mb4_0900_ai_ci`。ORM 为 SQLAlchemy 2.x（async），迁移由 Alembic 管理。

**已建立 15 张业务表**（架构文档 §7.2 中字段定义完整者）：

| 域 | 表 |
|---|---|
| 合同 | `contract`、`contract_file` |
| 审查任务 | `review_task` |
| 文档解析 | `document_block`、`contract_metadata`、`clause` |
| 规则 | `review_rule_set`、`review_rule` |
| 风险 | `risk_item`、`risk_suggestion` |
| 回写与日志 | `writeback_record`、`ai_call_log` |
| Mock 审批 | `approval_instance`、`approval_comment` |
| 同步游标 | `sync_cursor` |

**刻意未创建**（架构文档中无字段定义，留待对应阶段）：`sys_user`(P4)、`task_event`(P6)、`annotation`(P11)、`report`(P12)、`standard_clause`(P13)、`writeback_log`(P12)。

> ⚠️ `writeback_log` 在 P15 阶段正式被 `writeback_record` 替代：架构裁决明确"一份任务一份写回记录，不引入历史表"（`backend/app/services/writeback.py` 与 `backend/app/db/models/writeback.py` 的 docstring 同步说明）。若同一任务需要多条写回记录，由 `idempotency_key` 区分（同内容同 key → 复用；内容变化 → 新 key → 新行）。

**迁移**：baseline migration `313b0960b510`，位于 `backend/migrations/versions/`。以下命令均已实测通过（`upgrade` 建表 / `check` 与模型逐列一致 / `downgrade` 可逆）：

```bash
cd backend
uv run alembic upgrade head
uv run alembic check
uv run alembic downgrade base
```

**Seed**：`uv run python scripts/seed_rules.py` 灌入采购合同规则集（幂等且收敛，重复执行不产生重复行）。

| rule_code | 类型 | 等级 | 维度 |
|---|---|---|---|
| `IP_OWNER_SUPPLIER_001` | KEYWORD | HIGH | 知识产权 |
| `LIAB_UNLIMITED_001` | KEYWORD | HIGH | 违约责任 |
| `PAY_PREPAY_RATIO_001` | THRESHOLD | MEDIUM | 金额支付 |

> `MISSING`（必备条款缺失）类规则的 `expression` 契约在架构文档中没有示例，故**暂缓到 P9** 定义后再增量补充，当前只 seed 上述 3 条契约明确的规则。

**关键约定**：时间列一律 `DATETIME(3)` 存 naive UTC；金额一律 `DECIMAL(18,2)`（禁 float）；枚举字段存 VARCHAR（不用 MySQL 原生 ENUM）；外键一律无级联（RESTRICT）；数据库会话时区固定 `+00:00`。

---

## 合同接入（P4 完成）

入口 **`POST /api/v1/contracts`**（multipart 表单），一次请求产出 **Contract + ContractFile + ReviewTask** 三条记录。

### 分层

| 层 | 文件 | 只做什么 |
|---|---|---|
| HTTP | `backend/app/api/v1/endpoints/contracts.py` | 解析表单、按 1MiB 分块流式落临时文件（边写边判 50MB 上限）、组装响应。**零业务判断** |
| Service | `backend/app/services/contract_ingest.py` | 全部业务编排：校验 → 哈希 → 去重 → 建记录 → 落盘 → 补偿清理 |
| 响应 | `backend/app/schemas/contract.py` | 15 字段对外契约；**不返回** `storage_path` / 绝对路径 / 凭据 |

### 事务边界（P4 的核心设计）

文件系统与数据库不是同一个事务，因此**任何一段 DB 事务都不跨越文件 IO**：

```
1. 校验（纯本地）
2. 流式算 SHA-256（线程池）
3. T0：按 sha256 预查重（只读）      ── 命中 ⇒ 直接复用返回，不写任何东西
4. T1：创建 Contract → 取自增 id     ── 短事务，立即 commit
5. 原子落盘 uploads/{contract_id}/   ── 事务外，手里没有 DB Session
6. T2：ContractFile + ReviewTask + 回填 current_task_id ── 短事务
```

T1 与 T2 必须拆开：§7.2 规定最终路径含 `contract_id`，得先拿到自增主键才能确定路径；而文件移动又不能待在事务里。

### 文件校验（三层，缺一不可）

集中在 `backend/app/utils/file_utils.py`，**白名单只有一处定义**：

| 层 | 校验对象 | 为什么不能只靠它 |
|---|---|---|
| 扩展名 | 用户文件名后缀 | `evil.exe` 改名成 `a.pdf` 即可绕过 |
| MIME | 客户端声明的 Content-Type | 客户端可任意伪造 |
| **魔数** | 文件头字节 | —— **以此为准** |

唯一放宽处：浏览器对 DOCX 常发 `application/octet-stream`，此时允许退回按扩展名判定（`MIME_FALLBACK`），但魔数校验照做。

当前白名单：`.docx` `.pdf` `.jpg` `.jpeg` `.png` `.tif` `.tiff` `.bmp`（`code`：DOCX / PDF / JPEG / PNG / TIFF / BMP）。

> **待收窄**：已决定白名单最终只保留 `.docx` `.pdf` `.jpg` `.jpeg` `.png`，删除 `.tif` / `.tiff` / `.bmp`。该修改**尚未执行**，作为一个独立的小步骤处理（改 `file_utils.py` + 对应测试 + 全项目检查旧描述 + 跑完整 Backend 测试）。

### 幂等（两层，互相独立）

系统有**两层互相独立**的幂等，各自绑定在一个**已经存在的唯一约束**上：

| 层 | 回答的问题 | 判定依据 | 物理保障 | 响应字段 |
|---|---|---|---|---|
| **文件层** | 是不是同一个物理文件？ | `contract_file.sha256` | `uq_contract_file_sha256` | `reused` |
| **任务层** | 同一个 Contract + 同一个 File + **同一套审查配置**，是否已有任务？ | `review_task.idempotency_key` | `uq_review_task_idempotency_key` | `task_reused` |

任务层的幂等键沿用 §17.1 的公式：

```
idempotency_key = sha256(contract_id ‖ file_sha256 ‖ rule_set_version ‖ prompt_version)
```

`rule_set_version` 取该合同类型下**启用**规则集的 `version`（没有规则集时为 `"NONE"`，**不阻止上传**）；`prompt_version` 在 P4 尚无来源，由常量 `INGEST_ENGINE_VERSION = "ingest-v1"` 占位，P10 接入提示词后替换为真实版本。

**HTTP 状态码**：`200 ⟺ reused AND task_reused`，其余一律 `201`。即 201 表示"本次**创建了新东西**"—— 可能是新合同/附件，也可能是**在同一个合同下新建了一个审查任务**。

#### `reused` / `task_reused` 的组合

| `reused` | `task_reused` | 含义 | HTTP |
|---|---|---|---|
| `false` | `false` | 新文件、新任务 | 201 |
| `true` | `true` | 文件与任务均复用 | 200 |
| `true` | `false` | 复用了文件，但**审查配置不同** ⇒ 在同一 Contract + File 下**新建 ReviewTask** | 201 |
| `false` | `true` | 不可能出现（任务复用必然以文件复用为前提） | —— |

`reused=true`（文件层）的两个产生点：

| # | 产生点 | 触发条件 |
|---|---|---|
| 1 | `_reuse_existing_file()` 的 **T0 文件层命中** | 串行重复上传（最常见） |
| 2 | `_resolve_concurrent_duplicate()` | 并发同 sha256，T2 抛 `IntegrityError`，开**新事务**重查到胜出方 |

#### 任务层只看审查配置，**不看 status**

同一套审查配置就对应同一个任务，`pending` / `parsing` / `reviewing` / `blocked` / `completed` **一视同仁**：

- 命中即复用，**不修改它的 status**
- `completed` 是**终态**（§6.1「completed 不可回退」）—— 终态的含义是"**不能改它**"，而不是"不能返回它"。复用既不会复活它，也不会新建一个副本
- 要**重新审查**必须**改变审查配置**（新规则集版本 / 新 prompt 版本）⇒ 新键 ⇒ 新任务，旧任务按「历史任务全留存」原样保留
- 因此**同一 Contract + 同一 File 可以存在多个 ReviewTask**（例如 rule v1+prompt v1 / rule v2+prompt v1 / rule v2+prompt v2）。Schema 层面本就是 1:N 设计，只是 P4 此前只走了 1:1 的那条路

`contract.current_task_id` 只在**新建任务**时回填 —— 复用旧任务不会让它倒退。

> ⚠️ **当前没有 force rerun 入口**：审查配置完全相同时，重复上传一定会命中已有任务，只能拿到既有结果，无法强制重跑。若将来需要，可在幂等键中引入调用方提供的 `rerun_nonce`，或提供显式的 `force_rerun` 参数。P4 刻意不做。

> 实现说明：任务复用由 `review_task.idempotency_key` 的**精确匹配**决定，不使用"该合同下 id 最大的任务"这类近似代理 —— 早期版本的 `_latest_task()`（`ORDER BY id DESC LIMIT 1`，且不筛选 status）**已删除**。

### 并发（架构裁决 Q4）

`contract_file.sha256` 的 UNIQUE 约束是**最终一致性保障**。两个同内容请求同时进来时都会过 T0、都建自己的 Contract，最终只有一个能插入 `ContractFile`。输的一方捕获 `IntegrityError` → 事务已回滚 → **开新事务重新查询** → 查到即按复用返回。

不能"捕获异常后在原事务里重查"：MySQL 默认 REPEATABLE READ，原事务快照早于对方提交，**查不到**。

**任务层同理。** `review_task.idempotency_key` 的 UNIQUE 同样是最终保障：两个请求可能同时算出同一个键、同时查不到、同时尝试插入，只有一个能成功。输的一方由 `_reuse_file_with_task()` 捕获 `IntegrityError` → 事务已回滚 → **开新事务重新查询**，命中对方已提交的那一行 ReviewTask 并按复用返回（`task_reused=true`）。

`_T2DuplicateConflict` 与任务键冲突**必须区分**：前者是"文件层并发"，走整体复用胜出方的分支；后者在新建路径上是"确定性失败"，走补偿清理分支。因此 `ContractFile` 插入处的 `IntegrityError` 在**就地**被转换，而不再笼统捕获 —— 详见下方"补偿清理"。

### 补偿清理

补偿的前提是**能确定事务没有提交**，因此异常被刻意分成三类：

| 异常 | 语义 | 处理 |
|---|---|---|
| `_T2DuplicateConflict` | 并发同 sha256，确定未提交 | 删本次文件 + 删孤儿 Contract，复用胜出方 |
| `_T2DeterministicFailure` | commit **之前**失败，确定已回滚 | 删文件 + 条件式删 Contract，重抛原始异常 |
| 未包装的异常 | 只可能来自 commit/rollback 阶段，**结果不确定** | **禁止删除任何东西**，只记日志待人工协调 |

删 Contract 前逐条确认：`current_task_id IS NULL` + 无 `contract_file` 引用 + 无 `review_task` 引用，任一不满足即放弃删除。清理失败**只记 orphan 日志、不抛异常**（P4 不实现 GC）。

`contract_no` 冲突（同编号、不同内容）走 **409 CONFLICT**，与文件去重是两个完全不同的语义，测试中已显式防止二者被混淆。

---

## AI Agent 骨架（P5-1 ~ P5-3 完成）

Agent 是**独立 FastAPI 服务**（`127.0.0.1:8001`），是合同审查业务流程的核心；Backend（`127.0.0.1:8000`）退为数据层与存储层。

### 职责边界（架构约束，不可越界）

| 服务 | 负责 | **不负责** |
|---|---|---|
| Frontend | 展示、交互、调 API | 任何业务逻辑 |
| AI Agent | LangGraph 编排、State、节点、Parser、Prompt、LLM 调用 | 直连数据库 |
| Backend | FastAPI、MySQL、CRUD、文件存储、幂等、并发 | LangGraph、Prompt、LLM、Agent 编排 |

Agent **不引入 SQLAlchemy / aiomysql**（`ai-agent/pyproject.toml` 中刻意不声明），对 Backend 只通过 `httpx` 调领域 API。

### P5-3 的最小图

```
START
  │
  ▼
upload_file          ← 通过 Backend API 接入文件，不重实现校验/哈希/幂等
  │
  ▼
validate_file        ← Workflow Gate：判断上传产出是否完整到可继续
  │
  ├── invalid ──▶ END
  │
 valid
  ▼
parse_document       ← StubParser 桩实现，不产出正文
  │
  ▼
END
```

| 节点 | 类型 | 读 State | 写 State |
|---|---|---|---|
| `upload_file` | async | `file_path` `filename` `content_type` `contract_no` `title` `contract_type` | `contract_id` `file_id` `review_task_id` `sha256` `file_type` `file_size` `reused`；失败时 `error_code` `error_message` |
| `validate_file` | sync 纯函数 | 上述 upload 字段 + `error_code` | `file_valid` `validation_errors`；仅在"上传成功但产出不全"时补 `error_code` |
| `parse_document` | sync | `file_id` `file_type` | `parse_result`（`status="STUB"`、`text=""`） |

**Conditional Edge**（`app/graph/edges/routing.py`）依据唯一：`validate_file` 写入的 `file_valid`。判断提成**只读、无副作用**的纯函数，分支名到真实节点（含 `END`）的映射放在 `builder.py` 的 `path_map`。**fail-closed**：`file_valid` 缺失时走 `stop`，不默认放行。

**Graph 不抛业务异常**：上传失败是可预期的业务结果，写进 State 交给条件边分流。若抛异常会中断整个 Graph，条件边也就失去意义。

### 三个关键设计决策

1. **State 不是数据库副本**（`app/graph/state.py`）—— 只放跨节点流转需要的数据。不放 ORM 对象、不放 Backend 可重新查询的事实（`task_status` / `task_stage` 已刻意排除）。
   > ⚠️ **LangGraph 行为**：节点返回**未在 State Schema 中声明的键会被静默丢弃**，不报错。这是最容易写出"看起来跑了但没生效"的坑。
2. **依赖走 Runtime Context，不进 State**（`app/graph/context.py`）—— httpx client 是依赖不是业务数据。用 v1 的 `StateGraph(state_schema, context_schema=...)` + 节点签名 `(state, runtime)`，调用时 `ainvoke(..., context=ReviewContext(backend=...))` 注入。好处：测试换一个 client 就能跑，不需要 monkeypatch 全局单例。
3. **Parser 归属 `ai-agent/app/parsers/`**，Backend 的 `app/parsers/` 保持空目录。

### LangGraph 版本

使用 **langgraph 1.2.11（v1.x）**，**不沿用 0.x 教程的写法**。已实测的 API：

```python
StateGraph(state_schema, context_schema=...)   # v1 用 Runtime Context 做依赖注入
add_node(name, action)
add_edge(start_key, end_key)
add_conditional_edges(source, path, path_map)
compile()                                       # P5-3 不传 checkpointer
```

实测确认：`START == "__start__"`、`END == "__end__"`；`compile(checkpointer=None, *, cache=..., store=..., interrupt_before=..., ...)` —— `checkpointer` 是**位置参数**。

### P5-3 刻意不做

clauses / metadata / keywords / risks / suggestions / report / LLM / Prompt / Tool Calling / checkpoint / interrupt / 人工审核 / 数据库持久化 / 真实文档解析依赖（PyMuPDF、python-docx 均未安装）。

> 人工审核**不进 LangGraph**（不使用 `interrupt()` / checkpoint-resume）。Agent 跑完 `persist_result` 即 END，人工审核是 Backend 的普通业务状态：`risk_item.review_status`（PENDING / CONFIRMED / REJECTED / MODIFIED）用于单个风险项，`review_task.status`（pending / parsing / reviewing / blocked / completed）用于自动流程。

### 验证结果

```
ai-agent   uv run pytest -q        → 9 passed
           uv run ruff check .     → All checks passed
           uv run ruff format --check . → 26 files already formatted
backend    uv run pytest -q        → 342 passed
```

Agent 测试只 mock **网络层**（`httpx.MockTransport`），不 mock 自己的节点，因此 multipart 编码、响应解析、State 流转、Conditional Edge 分流全部真实执行。另做过一次**变异验证**：把路由函数临时改成恒返回 `continue` 后，非法文件场景下 `parse_document` 由"不执行"变为"执行"，证明条件边确实在控制流程。

---

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | **3.12.8** | 由 `.venv` 提供（uv 管理），非系统 3.13 |
| uv | 0.11.25 | 依赖管理工具，`uv.lock` 为锁定文件 |
| MySQL | 8.4.8 | 本机开发库 |
| Node.js | 24.x | 前端（P2-e 起使用） |

## 依赖安装

后端依赖声明在 `backend/pyproject.toml`，锁定在 `uv.lock`（**该文件必须提交**）。

```bash
# 方式一（推荐）：在 backend/ 目录下操作，uv 会自动使用仓库根目录的 .venv
cd backend
uv sync

# 方式二：在仓库根目录同步整个 workspace
uv sync --all-packages
```

> 本项目使用 **uv workspace**：仓库根目录的 `pyproject.toml` 是 workspace 根（不含依赖），
> 真实依赖在 `backend/pyproject.toml`，两者共用根目录下的同一个 `.venv`。

## 环境变量

```bash
cp .env.example .env
```

然后编辑 `.env` 填入本机数据库账号密码。

> ⚠️ `.env` 已被 `.gitignore` 忽略，**永远不会提交到 Git**。请勿把真实密码写入 `.env.example`、代码或日志。

## 目录结构

```
contract_approval/
├─ backend/                 # FastAPI 后端（数据层 + 存储层 + 基础能力层）
│  ├─ pyproject.toml        # 后端依赖声明
│  ├─ app/
│  │  ├─ core/              # 配置 / 常量枚举 / 日志 / 异常基座
│  │  ├─ db/                # 模型与数据访问（models / repositories）
│  │  ├─ schemas/           # Pydantic DTO
│  │  ├─ api/v1/endpoints/  # HTTP 接口层（当前：contracts.py）
│  │  ├─ services/          # 业务编排层（当前：contract_ingest.py）
│  │  ├─ storage/           # 存储抽象（base / local）——  P4 新增
│  │  ├─ parsers/           # 【空】文档解析归属已改到 ai-agent（见下）
│  │  ├─ llm/               # LLM Provider 抽象与提示词
│  │  ├─ rules/             # 规则引擎
│  │  ├─ integrations/      # 外部系统适配（审批系统）
│  │  ├─ reports/           # 报告渲染
│  │  ├─ workers/           # 异步任务执行
│  │  └─ utils/             # file_utils（三层校验）/ hash_utils（SHA256）/ …
│  ├─ scripts/              # 运维脚本（建库 / seed / 起 worker）
│  ├─ tests/                # unit / integration / fixtures
│  └─ storage/              # 运行时文件（已 gitignore）
├─ ai-agent/                # AI Agent 服务（LangGraph 编排，业务流程核心）
│  ├─ pyproject.toml        # Agent 依赖声明（刻意不含 SQLAlchemy / aiomysql）
│  ├─ app/
│  │  ├─ core/              # 配置（AgentSettings）/ 错误码（AgentErrorCode）
│  │  ├─ graph/
│  │  │  ├─ state.py        # ContractReviewState
│  │  │  ├─ context.py      # ReviewContext（Runtime Context 依赖注入）
│  │  │  ├─ builder.py      # StateGraph 装配与编译
│  │  │  ├─ nodes/          # upload_file / validate_file / parse_document
│  │  │  └─ edges/          # routing.py（Conditional Edge 判断函数）
│  │  ├─ tools/             # backend_client.py（Backend 领域 API 客户端）
│  │  ├─ schemas/           # ParseResult 等数据契约
│  │  ├─ parsers/           # 【待建】真实 Parser（P7）
│  │  ├─ llm/               # 【待建】LLM Provider（P10）
│  │  └─ services/          # 【待建】
│  └─ tests/                # unit / integration
├─ frontend/                # Vue3 + Vite 前端（P2-e 已完成基础壳工程）
├─ samples/                 # 示例合同
├─ docs/                    # 架构与阶段文档
├─ .env.example
└─ uv.lock                  # 依赖锁定文件
```

> 本仓库是 **uv workspace**，根 `pyproject.toml` 声明 `members = ["backend", "ai-agent"]`，
> 两个服务共享根目录下的同一个 `.venv`，但各自声明自己的依赖，互不污染。

## 安全约定

- 数据库密码、API Key 等敏感信息**只存放在本机 `.env`**，不进入代码、日志或版本库。
- `backend/storage/` 下的用户上传文件与渲染产物不进入版本库。

## 提交记录

| commit | 说明 |
|---|---|
| `1bb27db` | chore: initialize project and core infrastructure（P2-a / P2-b） |
| `ddec63b` | add database and application infrastructure（P2-c / P2-d） |
| `5c0f93a` | P2-e frontend shell（P2-e） |
| `b586b22` | feat: add contract review database models（P3） |
| `dc62d05` | docs: update project progress through P3（P3 收尾） |
| `c567c1d` | feat: complete contract ingestion and agent foundation（P4 / P5-1 ~ P5-3） |
| `5e34042` | docs: revise architecture for agent service（架构文档改版） |
| `2b78778` ~ `9c3bfc1` | 文档解析 → 条款 / 元数据 / 关键词（P6 / P7） |
| `8421e9a` ~ `eaeff0e` | 规则目录 API + 规则求值引擎 + 规则审查节点（P8） |
| `54f3640` ~ `8ed3c21` | LLM Provider / JSON 守卫 / quote 定位 / 统一风险模型 / 风险合并（P9） |
| `3086011` | feat: persist review risks to backend（P9-10） |
| `18b0352` | feat: complete document persistence pipeline（P10） |
| `e9e4f69` | feat: complete review workbench（P11） |
| `65a59a7` | feat: complete markdown review report export（P12） ← **当前 HEAD** |

> **P13（人工复核）的成果尚未提交**：将在 P13 整体收尾后一次性提交。
