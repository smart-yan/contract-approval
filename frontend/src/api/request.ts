/**
 * Axios 基础封装（架构文档 §2.2 request.ts）。
 *
 * 本阶段（P2-e）只做**基础骨架**：
 *   · baseURL / timeout
 *   · 请求拦截器：注入 X-Request-ID（链路追踪）
 *   · 响应拦截器：把后端的统一错误体归一化成 ApiError
 *
 * ⚠️ 刻意不做的事（属于后续业务阶段）：
 *   · JWT / 登录 / RBAC / Token 刷新 / 权限体系
 *   · 业务错误码 → 中文提示的完整映射表（后端 §8 定义了错误码，
 *     但"展示什么文案"是各业务模块的 UI 决策，不应耦合进 axios 层）
 *   · 自动弹 Element Plus 提示（本层保持与 UI 框架解耦）
 */

import axios, {
  type AxiosError,
  type AxiosInstance,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'

/** 业务接口基础路径，见 .env.development */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

/** 请求超时（毫秒）。与后端的长任务设计配套：长耗时操作后端返回 202 + task_id，不靠长连接等结果。 */
export const API_TIMEOUT_MS = 30_000

/** 后端统一错误体（架构文档 §8）：{ code, message, details, request_id } */
export interface ApiErrorBody {
  code: string
  message: string
  details?: Record<string, unknown>
  request_id?: string | null
}

/** 归一化后的错误。业务代码只需 `catch (e) { if (e instanceof ApiError) ... }`。 */
export class ApiError extends Error {
  readonly code: string
  readonly status: number | undefined
  readonly requestId: string | null
  readonly details: Record<string, unknown> | undefined

  constructor(init: {
    code: string
    message: string
    status?: number
    requestId?: string | null
    details?: Record<string, unknown>
  }) {
    super(init.message)
    this.name = 'ApiError'
    this.code = init.code
    this.status = init.status
    this.requestId = init.requestId ?? null
    this.details = init.details
  }
}

/** 生成 32 位十六进制请求 ID，与后端 `app/main.py` 生成格式一致。 */
export function createRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID().replace(/-/g, '')
  }
  // 兜底：老浏览器 / 受限环境没有 crypto.randomUUID
  return Math.random().toString(16).slice(2).padEnd(32, '0').slice(0, 32)
}

/** 从响应头取 request_id（后端回写 X-Request-ID，见 app/main.py 的中间件）。 */
function readResponseRequestId(response: AxiosResponse | undefined): string | null {
  const header = response?.headers?.['x-request-id']
  return typeof header === 'string' ? header : null
}

export const request: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: API_TIMEOUT_MS,
  headers: { 'Content-Type': 'application/json' },
})

request.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  // 链路追踪：后端中间件会生成/透传该头，并写入日志上下文与错误响应体。
  // 前端主动生成一个，使「浏览器里看到的 request_id」能直接在后端日志中检索到。
  config.headers.set('X-Request-ID', createRequestId())

  // TODO(P4)：认证接入后在此注入 Authorization 头。本阶段刻意不做认证体系。
  return config
})

request.interceptors.response.use(
  (response: AxiosResponse) => response,
  (error: AxiosError<ApiErrorBody>) => {
    const response = error.response
    const body = response?.data

    // 后端返回了统一错误体 → 用它的 code/message；否则是网络层错误 → 用 axios 的错误码
    const code = body?.code ?? (error.code === 'ECONNABORTED' ? 'REQUEST_TIMEOUT' : 'NETWORK_ERROR')
    const message = body?.message ?? error.message ?? '请求失败'

    return Promise.reject(
      new ApiError({
        code,
        message,
        status: response?.status,
        requestId: body?.request_id ?? readResponseRequestId(response),
        details: body?.details,
      }),
    )
  },
)

export default request
