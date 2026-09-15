import { afterEach, describe, expect, it, vi } from 'vitest'

import { request } from '@/api/request'
import { getReviewTaskWorkbench, type WorkbenchResponse } from '@/api/workbench'

afterEach(() => {
  vi.restoreAllMocks()
})

const SAMPLE: WorkbenchResponse = {
  task: {
    task_id: 42,
    status: 'pending',
    current_stage: 'REVIEWED',
    progress: 100,
    created_at: '2026-09-15T10:00:00',
    finished_at: null,
    risk_level_final: null,
    conclusion: null,
  },
  contract: {
    contract_id: 1,
    contract_no: 'HT-2026-001',
    title: '设备采购合同',
    contract_type: 'PURCHASE',
    our_party: '某某科技',
    counterparty: '乙方公司',
    amount: '1234.50',
    currency: 'CNY',
    sign_date: '2026-09-15',
    effective_date: null,
    expire_date: null,
    dept: '法务部',
  },
  file: {
    file_id: 7,
    file_name: 'contract.docx',
    file_type: 'DOCX',
    sha256: 'a'.repeat(64),
    parse_status: 'PARSED',
  },
  blocks: [],
  clauses: [],
  metadata: [],
  risks: [],
}

describe('getReviewTaskWorkbench()', () => {
  it('调用 Backend 的 GET /api/v1/review-tasks/{taskId}/workbench', async () => {
    // request 实例的 baseURL 是 /api/v1，因此这里传的是相对路径；
    // 拼出来是 /api/v1/review-tasks/42/workbench，由 vite 的 /api 代理转给 Backend。
    const spy = vi.spyOn(request, 'get').mockResolvedValue({ data: SAMPLE })

    await getReviewTaskWorkbench(42)

    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.calls[0][0]).toBe('/review-tasks/42/workbench')
  })

  it('taskId 原样拼进路径，不做额外包装', async () => {
    const spy = vi.spyOn(request, 'get').mockResolvedValue({ data: SAMPLE })

    await getReviewTaskWorkbench(987654)

    expect(spy.mock.calls[0][0]).toBe('/review-tasks/987654/workbench')
  })

  it('返回后端原始对象，字段一个不改', async () => {
    vi.spyOn(request, 'get').mockResolvedValue({ data: SAMPLE })

    await expect(getReviewTaskWorkbench(42)).resolves.toEqual(SAMPLE)
  })

  it('请求失败时把错误抛出去，由页面区分 404 与其它错误', async () => {
    vi.spyOn(request, 'get').mockRejectedValue(new Error('boom'))

    await expect(getReviewTaskWorkbench(42)).rejects.toThrow('boom')
  })
})
