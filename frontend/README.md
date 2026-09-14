# 合同审批审查系统 — 前端

Vue 3 + Vite + TypeScript + Element Plus 前端工程。

> 架构设计见 [`../docs/01-架构设计.md`](../docs/01-架构设计.md) 的 §2.2。
>
> **本 README 只记录已完成并验证过的内容。** 完整的部署与联调说明在 P16 阶段补齐。

---

## 当前阶段

**P2-e：前端基础壳工程**。

已具备：工程脚手架、路由、布局（侧边栏 + 顶栏 + 内容区）、Element Plus / Pinia / Vue Router 接入、
Axios 基础封装、404 页面、开发代理、基础测试。

**尚未实现任何业务功能** —— 合同、审查、风险、规则、报告等页面与接口全部属于后续阶段。

## 环境要求

| 组件 | 版本 |
|---|---|
| Node.js | 24.x（开发机实测 v24.14.0） |
| npm | 11.x（开发机实测 11.9.0） |

## 常用命令

```bash
npm install        # 安装依赖
npm run dev        # 启动开发服务器（默认 http://localhost:5173）
npm run build      # 类型检查 + 生产构建（输出到 dist/）
npm run preview    # 预览生产构建产物
npm run type-check # 仅做类型检查（vue-tsc --build）
npm test           # 运行单元测试（vitest run）
npm run test:watch # 单元测试 watch 模式
```

## 与后端的联调

开发期由 Vite 代理转发请求，**无需处理跨域**（见 `vite.config.ts` 的 `server.proxy`）：

| 前端路径 | 转发到 | 用途 |
|---|---|---|
| `/api/*` | `http://127.0.0.1:8000` | 业务接口（架构文档 §8 规定前缀 `/api/v1`） |
| `/health` | `http://127.0.0.1:8000` | 后端健康检查（挂在根路径，不属于业务接口） |

启动顺序：

```bash
# 终端 1：后端
cd ../backend && uv run uvicorn app.main:app --port 8000

# 终端 2：前端
npm run dev
```

打开 <http://localhost:5173> 即可看到「审查大盘」占位页，其中的**后端连通性**卡片会实时反映后端状态。

## 目录结构

```
frontend/
├─ index.html
├─ vite.config.ts          # 别名 @、开发代理、vitest 配置
├─ tsconfig.json           # 方案文件（引用下面两个）
├─ tsconfig.app.json       # src / tests 的编译配置（含 @ 路径别名）
├─ tsconfig.node.json      # vite.config.ts 自身的编译配置
├─ .env.development        # VITE_API_BASE_URL=/api/v1
├─ tests/unit/             # vitest 单元测试
└─ src/
   ├─ main.ts              # 应用入口：pinia → router → Element Plus
   ├─ App.vue              # 根组件（仅 <router-view />）
   ├─ env.d.ts             # import.meta.env 类型声明
   ├─ api/                 # 接口层
   │  ├─ request.ts        # Axios 实例：baseURL / 超时 / 请求与响应拦截器
   │  └─ system.ts         # 基础设施接口（健康检查）
   ├─ constants/app.ts     # 应用级常量
   ├─ layout/              # 布局
   │  ├─ DefaultLayout.vue
   │  └─ components/       # AppSidebar / AppHeader
   ├─ router/index.ts      # 路由 + 标题守卫
   ├─ stores/              # Pinia（index.ts 导出实例，app.ts 为外壳状态）
   ├─ styles/index.css     # 全局样式
   └─ views/
      ├─ dashboard/        # 审查大盘（占位页）
      └─ error/            # 404
```

> 架构文档 §2.2 里列出的 `composables/`、`components/document|risk|workspace`、
> `views/ContractList.vue` 等目录与文件属于后续业务阶段，**本阶段刻意未创建**，
> 以免出现"空壳占位"误导后来者以为功能已存在。

## 约定

- **路径别名**：`@` → `src`（`vite.config.ts` 与 `tsconfig.app.json` 两处需保持一致）。
- **组件库**：Element Plus 采用**全量引入**（`app.use(ElementPlus)`）。优点是配置简单、无额外构建插件；
  代价是打包体积偏大。若后续需要按需引入，可引入 `unplugin-vue-components` 改造 `vite.config.ts`。
- **不提前实现业务**：新增页面/接口前请先确认对应阶段，避免制造虚假的业务结构。
- **`X-Request-ID`**：`request.ts` 会为每个请求注入该头，后端会把同一个 id 写入日志与错误响应体，
  便于把浏览器侧的问题直接对应到后端日志。
