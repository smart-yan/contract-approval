import { describe, expect, it, vi, afterEach } from 'vitest'

import { getContracts, type ContractListItem } from '@/api/contracts'
import { request } from '@/api/request'
import { formatUtcTimestamp, parseUtcTimestamp } from '@/utils/datetime'

afterEach(() => {
  vi.restoreAllMocks()
})

function mockGet(data: unknown): ReturnType<typeof vi.spyOn> {
  const spy = vi.spyOn(request, 'get').mockResolvedValue({ data })
  return spy
}

describe('getContracts()', () => {
  it('调用 Backend 的 GET /api/v1/contracts', async () => {
    // request 实例的 baseURL 是 /api/v1（见 .env.development 与 request.ts），
    // 因此这里传相对路径 —— 拼出来就是 /api/v1/contracts，再由 vite 的 /api 代理转给 Backend。
    const spy = mockGet([])

    await getContracts()

    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.calls[0][0]).toBe('/contracts')
  })

  it('返回后端原始数组，不做包装或改名', async () => {
    const payload: ContractListItem[] = [
      {
        contract_id: 1,
        contract_no: 'HT-001',
        title: '采购合同',
        contract_type: 'PURCHASE',
        created_at: '2026-09-15T10:00:00',
        latest_task: { task_id: 7, status: 'pending', current_stage: 'REVIEWED', progress: 100 },
      },
    ]
    mockGet(payload)

    await expect(getContracts()).resolves.toEqual(payload)
  })

  it('空数组原样返回（"暂无合同"是结论，不是错误）', async () => {
    mockGet([])

    await expect(getContracts()).resolves.toEqual([])
  })

  it('请求失败时把错误抛出去，由调用方展示', async () => {
    vi.spyOn(request, 'get').mockRejectedValue(new Error('boom'))

    await expect(getContracts()).rejects.toThrow('boom')
  })
})

describe('parseUtcTimestamp()：naive UTC 必须按 UTC 解释', () => {
  it('把不带时区的串当成 UTC，而不是本地时间', () => {
    // 这是本模块存在的**全部理由**。ECMAScript 规定：不带时区标识的日期时间串
    // 按**本地**时间解析 —— 于是 new Date('2026-09-15T10:00:00') 在东八区
    // 会被当成北京时间 10:00，比真实时刻早 8 小时，且不报任何错。
    const parsed = parseUtcTimestamp('2026-09-15T10:00:00')

    expect(parsed?.getTime()).toBe(Date.UTC(2026, 8, 15, 10, 0, 0))
  })

  it('已经带时区标识的串原样解析（幂等）', () => {
    const withZ = parseUtcTimestamp('2026-09-15T10:00:00Z')

    expect(withZ?.getTime()).toBe(Date.UTC(2026, 8, 15, 10, 0, 0))
  })

  it('解析不出来时返回 null，不抛异常', () => {
    expect(parseUtcTimestamp('没这回事')).toBeNull()
    expect(parseUtcTimestamp('')).toBeNull()
    expect(parseUtcTimestamp(null)).toBeNull()
    expect(parseUtcTimestamp(undefined)).toBeNull()
  })
})

describe('formatUtcTimestamp()', () => {
  it('输出 YYYY-MM-DD HH:mm，且与本地时区的换算结果一致', () => {
    const formatted = formatUtcTimestamp('2026-09-15T10:00:00')
    const expected = parseUtcTimestamp('2026-09-15T10:00:00')!

    // 断言的不是某个固定的字面量（那会依赖运行机器的时区），
    // 而是"用的是同一个时刻 + 按本地时区格式化"这个语义。
    expect(formatted).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
    expect(formatted).toContain(String(expected.getFullYear()))
    expect(formatted).toContain(String(expected.getDate()).padStart(2, '0'))
  })

  it('解析不出来时原样返回，不显示 Invalid Date', () => {
    expect(formatUtcTimestamp('没这回事')).toBe('没这回事')
    expect(formatUtcTimestamp(null)).toBe('')
  })
})
