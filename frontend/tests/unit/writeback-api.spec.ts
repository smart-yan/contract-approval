/**
 * 写回 API 客户端测试。
 *
 * 对应 :mod:`@/api/writeback` 的契约：
 * * POST 到 ``/api/v1/review-tasks/{taskId}/writeback``（**无** request body）
 * * **不**发 ``approval_instance_id`` / ``idempotency_key``
 * * 返回值是 Backend ``WritebackRunResponse`` 的字段
 *
 * 与 ``risk-review-api.spec.ts`` 是同一套写法：mock `request.post`，断言 URL、
 * 字段透传、错误处理。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, request } from '@/api/request'
import { postWriteback, type WritebackResult } from '@/api/writeback'

afterEach(() => {
  vi.restoreAllMocks()
})

const SUCCESS: WritebackResult = {
  record_id: 1,
  task_id: 42,
  approval_instance_id: 7,
  status: 'success',
  attempt: 1,
  external_comment_id: 'MOCK-CMT-100',
  error_msg: null,
  finished_at: '2026-09-16T10:00:00',
  posted: true,
}

const FAILED: WritebackResult = {
  record_id: 2,
  task_id: 42,
  approval_instance_id: 7,
  status: 'failed',
  attempt: 2,
  external_comment_id: null,
  error_msg: '审批系统不可达',
  finished_at: '2026-09-16T10:01:00',
  posted: true,
}

function mockPost(data: WritebackResult) {
  return vi.spyOn(request, 'post').mockResolvedValue({ data })
}

describe('postWriteback()', () => {
  it('用 POST 打到 /review-tasks/{taskId}/writeback（不带 body）', async () => {
    const spy = mockPost(SUCCESS)

    await postWriteback(42)

    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.calls[0]![0]).toBe('/review-tasks/42/writeback')
    // 不发 approval_instance_id / idempotency_key / 任何 body 字段
    expect(spy.mock.calls[0]![1]).toBeUndefined()
  })

  it('taskId 进 URL，不做包装', async () => {
    const spy = mockPost(SUCCESS)

    await postWriteback(987654)

    expect(spy.mock.calls[0]![0]).toBe('/review-tasks/987654/writeback')
  })

  it('成功响应字段一个不改地透传', async () => {
    mockPost(SUCCESS)

    await expect(postWriteback(42)).resolves.toEqual(SUCCESS)
  })

  it('失败响应（HTTP 200 + status=failed）也透传 —— 不是 HTTP 错误', async () => {
    mockPost(FAILED)

    await expect(postWriteback(42)).resolves.toEqual(FAILED)
  })

  it('把 ApiError 原样抛出去，由调用方按 409 / 404 / 网络区分提示', async () => {
    vi.spyOn(request, 'post').mockRejectedValue(
      new ApiError({
        code: 'WRITEBACK_ALREADY_SUCCESS',
        message: '该意见已成功回写，无需重复提交',
        status: 409,
      }),
    )

    await expect(postWriteback(42)).rejects.toBeInstanceOf(ApiError)
  })

  it('409 WRITEBACK_APPROVAL_NOT_READY 也是业务错误，原样抛出', async () => {
    vi.spyOn(request, 'post').mockRejectedValue(
      new ApiError({
        code: 'WRITEBACK_APPROVAL_NOT_READY',
        message: '该合同尚未关联审批单，无法回写审批意见',
        status: 409,
      }),
    )

    await expect(postWriteback(42)).rejects.toBeInstanceOf(ApiError)
  })
})