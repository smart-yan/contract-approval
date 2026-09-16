/**
 * 发起一次合同审查（P14-5-1）。
 *
 * 对应 **Agent 服务**的 ``POST /api/agent/review``（P14-4 起是**异步受理**）。
 *
 * ⚠️ 这是前端唯一一个**不打 Backend** 的业务调用 —— 它打的是 Agent（独立服务，
 * 开发期 127.0.0.1:8001）。两者是不同进程、不同前缀（Backend 是 ``/api/v1``，
 * Agent 是 ``/api/agent``），因此这里显式换掉 baseURL，而不是把 Agent 的路由
 * 混进 ``/api/v1`` 的代理里。
 *
 * 异步受理意味着什么
 * ----------------
 * ::
 *
 *     POST /api/agent/review  →  202 {task_id}      ← 只表示"受理了"
 *                                        │
 *                                        ▼
 *                              图在 Agent 进程的后台跑
 *                                        │
 *                                        ▼
 *                        真实状态在 Backend 的 ReviewTask 上
 *
 * **202 不代表审查成功**，它连"这份文件能审"都还没判断（解析在后台做）。
 * 因此调用方拿到 ``task_id`` 后要做的是**跳去工作台看真实状态**，而不是
 * 在这里等结果 —— 本模块刻意不提供"上传完就等出结果"的写法。
 */

import { ApiError, request } from '@/api/request'

/**
 * Agent 服务的接口前缀。
 *
 * 开发期由 vite 的 ``server.proxy`` 转发到 Agent（见 vite.config.ts 的
 * ``/api/agent`` 规则），因此这里用相对路径，无需处理跨域。
 */
export const AGENT_BASE_URL: string = import.meta.env.VITE_AGENT_BASE_URL ?? '/api/agent'

/** 发起审查所需的表单。字段名必须与 Agent 端点的 ``Form(...)`` 参数**逐字对应**。 */
export interface ReviewStartRequest {
  /** 合同文件。黄金链路只支持 DOCX（其余类型由 Agent 的门禁拒绝） */
  file: File
  contractNo: string
  title: string
  contractType: string
}

/** 受理成功的响应体（Agent 的 ``ReviewAcceptedResponse``）。 */
export interface ReviewAccepted {
  /**
   * Backend 的 **真实** ReviewTask ID —— 由 Agent 在请求内预上传时创建。
   * 拿它去 ``GET /api/v1/review-tasks/{task_id}/workbench`` 看进度与结果。
   */
  task_id: number
}

/**
 * 文件大小上限提示用的文案来源。
 *
 * ⚠️ 前端**不做**尺寸/类型的权威校验 —— 那是 Backend 与 Agent 门禁的职责
 * （契约只有一处）。这里只用于 ``accept`` 提示，避免用户白传一个大文件。
 */
export const ACCEPTED_UPLOAD_EXTENSIONS = '.docx'

/**
 * 提交一份合同，开始审查。
 *
 * :returns: 受理结果（含 ``task_id``）。**它不代表审查已完成**。
 * :raises ApiError:
 *   * 启动阶段就被拒（422）→ ``code`` 是 Agent 的错误码（如 ``PARSE_UNSUPPORTED_TYPE``、
 *     ``BACKEND_REJECTED``、``BACKEND_UNREACHABLE``），``message`` 是 Agent 给的人话原因
 *   * 表单本身不合法（FastAPI 的 422 校验）→ ``code`` 回落为 ``NETWORK_ERROR``，
 *     此时 ``status === 422``，调用方据此区分"参数不合法"与"Agent 拒绝了这次审查"
 */
export async function startContractReview(input: ReviewStartRequest): Promise<ReviewAccepted> {
  const form = new FormData()
  form.append('file', input.file)
  form.append('contract_no', input.contractNo)
  form.append('title', input.title)
  form.append('contract_type', input.contractType)

  const { data } = await request.post<ReviewAccepted>('/review', form, {
    baseURL: AGENT_BASE_URL,
    // ⚠️ 必须显式覆盖。axios 实例的默认头是 ``application/json``，而它见到
    // FormData 时会**把它 JSON 序列化**（axios 1.x 的 transformRequest 分支），
    // 文件会因此变成一段无意义的 JSON。写成 multipart 后 axios 会把这一行交给
    // 浏览器，由浏览器补上带 boundary 的完整头。
    headers: { 'Content-Type': 'multipart/form-data' },
  })

  if (typeof data?.task_id !== 'number') {
    // 202 但没给 task_id = 拿不到"接下来看哪个任务"。放行会让页面跳到一个
    // 拼不出 taskId 的 URL，用户看到的是"任务不存在"——而任务是刚刚建出来的。
    throw new ApiError({
      code: 'REVIEW_ACCEPTED_WITHOUT_TASK_ID',
      message: 'Agent 受理了这次审查，但响应里没有 task_id，无法查看进度',
      status: 202,
    })
  }

  return data
}
