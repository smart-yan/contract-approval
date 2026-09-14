# 合同审批审查系统（Contract Approval & Review System）

面向企业合同审批场景的智能审查系统：支持多格式合同解析与条款结构化切分，通过**合规规则引擎 + 大语言模型**识别法律与商业风险，提供原文高亮定位、修改建议与报告导出，并支持将审查意见回写至审批系统评论区。

> 架构设计见 [`docs/01-架构设计.md`](docs/01-架构设计.md)。
>
> **本 README 当前只记录已完成并验证过的内容。** 完整的启动、部署与接口文档在 P16 阶段补齐。

---

## 当前阶段进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1 | 架构设计 | ✅ 已完成（架构文档已确认） |
| **P2-a** | **版本控制 / 依赖 / 目录骨架** | **✅ 已完成** |
| P2-b | core 基座（config / constants / logging / errors） | ⏳ 进行中 |
| P2-c | 数据库与 Alembic 初始化 | ⏸ 未开始 |
| P2-d | 并发模型基座 + FastAPI 入口 + `/health` | ⏸ 未开始 |
| P2-e | 前端空壳（Vite + Vue3 + Element Plus） | ⏸ 未开始 |

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
├─ frontend/                # Vue3 + Vite 前端（P2-e 起）
├─ samples/                 # 示例合同
├─ docs/                    # 架构与阶段文档
├─ .env.example
└─ uv.lock                  # 依赖锁定文件
```

## 安全约定

- 数据库密码、API Key 等敏感信息**只存放在本机 `.env`**，不进入代码、日志或版本库。
- `backend/storage/` 下的用户上传文件与渲染产物不进入版本库。
