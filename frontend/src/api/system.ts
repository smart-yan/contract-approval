/**
 * 基础设施接口：后端健康检查。
 *
 * ⚠️ 这不是业务接口。后端的 `/health` 挂在**根路径**上，刻意不属于 `/api/v1`
 * （见 backend/app/api/health.py），因此这里不复用 request.ts 的 `/api/v1` 实例，
 * 直接用相对路径走 vite.config.ts 里的 `/health` 代理。
 *
 * 存在的意义：让壳工程具备一个**真实可验证的前后端连通点**，
 * 而不是只靠"配置看起来对"来证明 Axios 可用。
 */

import axios from 'axios'

export interface DatabaseCheck {
  ok: boolean
  database: string | null
  server_version: string | null
  charset: string | null
  latency_ms: number | null
  error_code: string | null
  error_type: string | null
}

export interface ExecutorCheck {
  thread_pool_created: boolean
  thread_pool_size: number
  ocr_executor_created: boolean
  ocr_executor_type: string | null
  ocr_executor_mode: string
  ocr_max_workers: number
}

export interface HealthResponse {
  status: 'ok' | 'degraded'
  app_env: string
  version: string
  uptime_seconds: number | null
  checks: {
    database: DatabaseCheck
    executors: ExecutorCheck
  }
}

/** 健康检查的超时比业务接口短：探测类请求宁可快速失败。 */
const HEALTH_TIMEOUT_MS = 5_000

export async function fetchHealth(): Promise<HealthResponse> {
  const { data } = await axios.get<HealthResponse>('/health', { timeout: HEALTH_TIMEOUT_MS })
  return data
}
