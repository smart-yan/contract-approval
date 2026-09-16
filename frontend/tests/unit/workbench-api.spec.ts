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

/** 一条**已复核**的风险 —— 复核四列都带实际值（P13-2 新增的 DTO 字段）。 */
const REVIEWED_RISK: WorkbenchResponse['risks'][number] = {
  risk_id: 900,
  risk_code: 'IP_OWNER_SUPPLIER_001',
  risk_title: '知识产权归属相对方',
  dimension: '知识产权',
  risk_level: 'LOW',
  source: 'RULE',
  reason: '成果归属供方会限制我方后续使用。',
  legal_basis: null,
  original_text: '知识产权归乙方',
  paragraph_index: 23,
  clause_id: 100,
  locator_type: 'PARAGRAPH',
  review_status: 'MODIFIED',
  reviewer_id: null,
  review_comment: '等级下调',
  reviewed_at: '2026-09-16T08:12:34.567',
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

  it('复核字段原样透传：PENDING 是三个 null，已复核带实际值（P13-2）', async () => {
    // 「未复核」与「已复核」是前端必须能表达的两态。这条用例的价值不在断言本身
    // （本层就是个透传），而在于**它必须通过类型检查** —— 两个样本都写成
    // ``WorkbenchResponse``，少一个复核字段 ``npm run build`` 就会失败。
    const pendingRisk = { ...REVIEWED_RISK, review_status: 'PENDING', review_comment: null, reviewed_at: null }
    const reviewedRisk = { ...REVIEWED_RISK }

    vi.spyOn(request, 'get').mockResolvedValue({
      data: { ...SAMPLE, risks: [pendingRisk, reviewedRisk] },
    })

    const result = await getReviewTaskWorkbench(42)

    // 未复核：三个复核列都是 null，且 reviewer_id 与复核与否无关（恒 null）
    expect(result.risks[0]!.reviewer_id).toBeNull()
    expect(result.risks[0]!.review_comment).toBeNull()
    expect(result.risks[0]!.reviewed_at).toBeNull()
    // 已复核：实际值原样带回，时间串**不做任何格式化**
    expect(result.risks[1]!.review_status).toBe('MODIFIED')
    expect(result.risks[1]!.review_comment).toBe('等级下调')
    expect(result.risks[1]!.reviewed_at).toBe('2026-09-16T08:12:34.567')
    expect(result.risks[1]!.reviewer_id).toBeNull()
  })

  it('请求失败时把错误抛出去，由页面区分 404 与其它错误', async () => {
    vi.spyOn(request, 'get').mockRejectedValue(new Error('boom'))

    await expect(getReviewTaskWorkbench(42)).rejects.toThrow('boom')
  })
})
