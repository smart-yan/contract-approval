/**
 * ``POST /api/agent/review`` 的前端封装（P14-5-1）。
 *
 * ⚠️ 这里**不 mock 自己的 api 模块**（那样等于把要测的东西换掉），而是换掉 axios 的
 * **adapter** —— 请求照样走完整的拦截器链（X-Request-ID 注入、错误体归一化），
 * 只是不出网。这样"202 怎么解析""Agent 的拒绝体怎么变成 ApiError"两条都是**真跑**的。
 */
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it } from 'vitest'

import { AGENT_BASE_URL, startContractReview, type ReviewAccepted } from '@/api/agent-review'
import { ApiError, request } from '@/api/request'

const originalAdapter = request.defaults.adapter

/** 最近一次请求的 config —— 用来断言"到底发出去了什么"。 */
let lastConfig: InternalAxiosRequestConfig | undefined

/** 让下一次请求成功返回（响应体 + 状态码）。 */
function respondWith(body: unknown, status = 202): void {
  request.defaults.adapter = async (config) => {
    lastConfig = config
    return { data: body, status, statusText: 'Accepted', headers: {}, config }
  }
}

/**
 * 让下一次请求**失败**并带上响应体。
 *
 * ⚠️ 必须抛 ``AxiosError`` 且带 ``response``：拦截器是靠 ``error.response.data``
 * 归一化错误体的，抛一个普通 Error 只会得到 NETWORK_ERROR，测不到真实路径。
 */
function rejectWith(body: unknown, status: number): void {
  request.defaults.adapter = async (config) => {
    lastConfig = config
    throw new AxiosError(`Request failed with status code ${status}`, 'ERR_BAD_REQUEST', config, {}, {
      data: body,
      status,
      statusText: 'Unprocessable Entity',
      headers: {},
      config,
    })
  }
}

function sampleFile(): File {
  return new File([new Uint8Array([1, 2, 3])], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  })
}

function submit(file: File = sampleFile()): Promise<ReviewAccepted> {
  return startContractReview({
    file,
    contractNo: 'HT-2026-001',
    title: '设备采购合同',
    contractType: 'PURCHASE',
  })
}

afterEach(() => {
  request.defaults.adapter = originalAdapter
  lastConfig = undefined
})

describe('startContractReview()', () => {
  it('打的是 Agent 服务：POST /api/agent/review', async () => {
    respondWith({ task_id: 42 })

    await submit()

    expect(lastConfig?.method).toBe('post')
    expect(lastConfig?.url).toBe('/review')
    // ⚠️ baseURL 必须换成 Agent 的前缀 —— 用 /api/v1 会打到 Backend（那边没有这个路由）
    expect(lastConfig?.baseURL).toBe(AGENT_BASE_URL)
    expect(AGENT_BASE_URL).toBe('/api/agent')
  })

  it('以 multipart 提交四个字段，文件是原始 File 对象', async () => {
    respondWith({ task_id: 42 })
    const file = sampleFile()

    await submit(file)

    const body = lastConfig?.data as FormData
    expect(body).toBeInstanceOf(FormData)
    // 字段名与 Agent 端点的 Form(...) 参数逐字对应，错一个就是 422
    expect(body.get('contract_no')).toBe('HT-2026-001')
    expect(body.get('title')).toBe('设备采购合同')
    expect(body.get('contract_type')).toBe('PURCHASE')
    expect(body.get('file')).toBe(file)
    // 实例默认头是 application/json，必须覆盖，否则 axios 会把 FormData 序列化成 JSON
    expect(String(lastConfig?.headers?.['Content-Type'])).toContain('multipart/form-data')
  })

  it('202 时取出 task_id（**不是**审查结果）', async () => {
    respondWith({ task_id: 987654 })

    await expect(submit()).resolves.toEqual({ task_id: 987654 })
  })

  it('202 但没给 task_id → 如实报错，返回一个拼不出 URL 的结果', async () => {
    respondWith({})

    const error = await submit().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('REVIEW_ACCEPTED_WITHOUT_TASK_ID')
    expect((error as ApiError).status).toBe(202)
  })

  it('Agent 启动阶段拒绝（422）→ 保留 Agent 的错误码与人话原因', async () => {
    // Agent 的响应体（ReviewRunResponse）：error_code / error_message，**不是**
    // Backend 那套 code / message
    rejectWith(
      {
        workflow_status: 'rejected',
        error_code: 'PARSE_UNSUPPORTED_TYPE',
        error_message: '不支持的文件类型',
      },
      422,
    )

    const error = (await submit().catch((caught: unknown) => caught)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('PARSE_UNSUPPORTED_TYPE')
    expect(error.message).toBe('不支持的文件类型')
    expect(error.status).toBe(422)
  })

  it('FastAPI 的请求体校验（422 + detail）与 Agent 的业务拒绝可区分', async () => {
    // 这类响应体没有业务错误码 → 拦截器回落成 NETWORK_ERROR。
    // 页面据 status === 422 && code === 'NETWORK_ERROR' 判断"参数不合法"，
    // 而不是把用户引到"网络错误"上去排查。
    rejectWith({ detail: [{ loc: ['body', 'title'], msg: 'Field required' }] }, 422)

    const error = (await submit().catch((caught: unknown) => caught)) as ApiError

    expect(error.code).toBe('NETWORK_ERROR')
    expect(error.status).toBe(422)
  })

  it('Agent 连不上（没有 response）→ NETWORK_ERROR', async () => {
    request.defaults.adapter = async (config) => {
      lastConfig = config
      throw new AxiosError('Network Error', 'ERR_NETWORK', config, {})
    }

    const error = (await submit().catch((caught: unknown) => caught)) as ApiError

    expect(error.code).toBe('NETWORK_ERROR')
    expect(error.status).toBeUndefined()
  })
})
