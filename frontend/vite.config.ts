import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
// 从 vitest/config 引入 defineConfig，才能在同一份配置里写 test 段
import { defineConfig } from 'vitest/config'

/** 后端服务地址。与 backend/ 的 uvicorn 默认端口一致（uvicorn app.main:app --port 8000）。 */
const BACKEND_ORIGIN = 'http://127.0.0.1:8000'

/**
 * AI Agent 服务地址。架构文档 §2 规定它是**独立服务**（127.0.0.1:8001），
 * 只有 ``POST /api/agent/review`` 这一个入口是前端要打的（P14-5-1）。
 */
const AGENT_ORIGIN = 'http://127.0.0.1:8001'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],

  resolve: {
    // 与 tsconfig.app.json 的 paths 保持一致
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    port: 5173,
    proxy: {
      // ⚠️ **顺序有意义**：vite 按声明顺序做**前缀匹配**，谁先命中谁生效。
      // /api/agent 必须排在 /api 前面，否则它会被那条更宽的规则截走、
      // 转去 Backend（那里没有这个路由，结果是 404）。
      //
      // Agent 的接口前缀是 /api/agent（架构文档 §2：Agent 是独立服务，
      // 审查入口 POST /api/agent/review 归它）。
      '/api/agent': { target: AGENT_ORIGIN, changeOrigin: true },
      // 业务接口：架构文档 §8 规定统一前缀 /api/v1
      '/api': { target: BACKEND_ORIGIN, changeOrigin: true },
      // 基础设施端点：后端 /health 挂在根路径，刻意不属于业务接口（见 backend/app/api/health.py）
      '/health': { target: BACKEND_ORIGIN, changeOrigin: true },
    },
  },

  test: {
    environment: 'jsdom',
    include: ['tests/**/*.spec.ts'],
  },
})
