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
| **P3** | **后端数据库建模（15 张业务表 + baseline 迁移 + 规则 seed）** | **✅ PASS** |

> **当前状态：P3 已完成，工作区 clean，尚未进入 P4。**
>
> 尚未实现的业务：合同接入、文档解析、OCR、规则引擎、LLM 审查、任务 Worker、人工复核、审批回写、报告导出。

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
├─ backend/                 # FastAPI 后端
│  ├─ pyproject.toml        # 后端依赖声明
│  ├─ app/
│  │  ├─ core/              # 配置 / 常量枚举 / 日志 / 异常基座
│  │  ├─ db/                # 模型与数据访问（models / repositories）
│  │  ├─ schemas/           # Pydantic DTO
│  │  ├─ api/v1/endpoints/  # HTTP 接口层
│  │  ├─ services/          # 业务编排层
│  │  ├─ parsers/           # 文档解析（DOCX / PDF / OCR）
│  │  ├─ llm/               # LLM Provider 抽象与提示词
│  │  ├─ rules/             # 规则引擎
│  │  ├─ integrations/      # 外部系统适配（审批系统）
│  │  ├─ reports/           # 报告渲染
│  │  ├─ workers/           # 异步任务执行
│  │  └─ utils/
│  ├─ scripts/              # 运维脚本（建库 / seed / 起 worker）
│  ├─ tests/                # unit / integration / fixtures
│  └─ storage/              # 运行时文件（已 gitignore）
├─ frontend/                # Vue3 + Vite 前端（P2-e 已完成基础壳工程）
├─ samples/                 # 示例合同
├─ docs/                    # 架构与阶段文档
├─ .env.example
└─ uv.lock                  # 依赖锁定文件
```

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
