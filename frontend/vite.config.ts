import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
// 从 vitest/config 引入 defineConfig，才能在同一份配置里写 test 段
import { defineConfig } from 'vitest/config'

/** 后端服务地址。与 backend/ 的 uvicorn 默认端口一致（uvicorn app.main:app --port 8000）。 */
const BACKEND_ORIGIN = 'http://127.0.0.1:8000'

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
