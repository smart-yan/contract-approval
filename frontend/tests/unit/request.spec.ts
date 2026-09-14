import { describe, expect, it } from 'vitest'

import { ApiError, API_BASE_URL, API_TIMEOUT_MS, createRequestId, request } from '@/api/request'

describe('Axios 基础封装', () => {
  it('baseURL 指向 /api/v1（架构文档 §8 的统一前缀）', () => {
    expect(API_BASE_URL).toBe('/api/v1')
    expect(request.defaults.baseURL).toBe(API_BASE_URL)
  })

  it('配置了超时', () => {
    expect(API_TIMEOUT_MS).toBeGreaterThan(0)
    expect(request.defaults.timeout).toBe(API_TIMEOUT_MS)
  })

  it('注册了请求拦截器与响应拦截器', () => {
    // handlers 在类型上是可选的，取不到时按空数组处理
    const requestHandlers = request.interceptors.request.handlers ?? []
    const responseHandlers = request.interceptors.response.handlers ?? []

    // 请求拦截器负责注入 X-Request-ID
    expect(requestHandlers.filter(Boolean)).not.toHaveLength(0)
    // 响应拦截器负责把后端错误体归一化成 ApiError
    expect(responseHandlers.filter(Boolean)).not.toHaveLength(0)
  })

  it('createRequestId 生成 32 位十六进制字符串（与后端格式一致）', () => {
    const id = createRequestId()
    expect(id).toMatch(/^[0-9a-f]{32}$/)
  })

  it('createRequestId 每次结果不同', () => {
    const ids = new Set(Array.from({ length: 50 }, () => createRequestId()))
    expect(ids.size).toBe(50)
  })

  it('ApiError 保留后端返回的 code / status / request_id', () => {
    const error = new ApiError({
      code: 'DATABASE_UNAVAILABLE',
      message: '数据库暂时不可用',
      status: 503,
      requestId: 'abcdef0123456789abcdef0123456789',
      details: { database: 'contract_approval' },
    })

    expect(error).toBeInstanceOf(Error)
    expect(error.name).toBe('ApiError')
    expect(error.code).toBe('DATABASE_UNAVAILABLE')
    expect(error.status).toBe(503)
    expect(error.requestId).toBe('abcdef0123456789abcdef0123456789')
    expect(error.details).toEqual({ database: 'contract_approval' })
  })

  it('ApiError 未提供 requestId 时回落为 null', () => {
    const error = new ApiError({ code: 'NETWORK_ERROR', message: '网络异常' })
    expect(error.requestId).toBeNull()
    expect(error.status).toBeUndefined()
  })
})
