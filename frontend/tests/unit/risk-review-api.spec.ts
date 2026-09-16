import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, request } from '@/api/request'
import { reviewRiskItem } from '@/api/risk-review'

afterEach(() => {
  vi.restoreAllMocks()
})

const RESULT = {
  risk_id: 900,
  task_id: 42,
  risk_level: 'LOW',
  review_status: 'MODIFIED',
  review_comment: '等级下调',
  reviewer_id: null,
  reviewed_at: '2026-09-16T08:12:34.567',
}

function mockPatch() {
  return vi.spyOn(request, 'patch').mockResolvedValue({ data: RESULT })
}

describe('reviewRiskItem()', () => {
  it('用 PATCH 打到 /review-tasks/{taskId}/risks/{riskId}', async () => {
    const spy = mockPatch()

    await reviewRiskItem(42, 900, { review_status: 'CONFIRMED' })

    expect(spy).toHaveBeenCalledTimes(1)
    // baseURL 是 /api/v1，拼接后由 vite 的 /api 代理转给 Backend
    expect(spy.mock.calls[0][0]).toBe('/review-tasks/42/risks/900')
  })

  it('taskId 与 riskId 都进 URL，不做包装', async () => {
    const spy = mockPatch()

    await reviewRiskItem(987654, 321, { review_status: 'REJECTED' })

    expect(spy.mock.calls[0][0]).toBe('/review-tasks/987654/risks/321')
  })

  it('返回后端原始对象，字段一个不改', async () => {
    mockPatch()

    await expect(reviewRiskItem(42, 900, { review_status: 'MODIFIED' })).resolves.toEqual(RESULT)
  })

  it('把 ApiError 原样抛出去，由页面区分 409 / 404 / 422', async () => {
    vi.spyOn(request, 'patch').mockRejectedValue(
      new ApiError({ code: 'RISK_REVIEW_NOT_READY', message: '任务尚未完成风险审查', status: 409 }),
    )

    await expect(reviewRiskItem(42, 900, { review_status: 'CONFIRMED' })).rejects.toBeInstanceOf(
      ApiError,
    )
  })
})
