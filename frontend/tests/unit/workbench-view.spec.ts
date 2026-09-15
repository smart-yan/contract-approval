import { mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

import { ApiError } from '@/api/request'
import type { WorkbenchResponse } from '@/api/workbench'
import { routes } from '@/router'
import WorkbenchView from '@/views/workbench/WorkbenchView.vue'

const getWorkbenchMock = vi.fn()
vi.mock('@/api/workbench', () => ({
  getReviewTaskWorkbench: (taskId: number) => getWorkbenchMock(taskId),
}))

/**
 * jsdom **没有实现** ``Element.prototype.scrollIntoView``（不是实现了但什么都不做，
 * 是压根没有这个方法），不补桩调用就会直接抛 TypeError。
 *
 * 补的是**测试环境的空缺**，不是生产行为：页面该调还是照调，只是这里不真滚动。
 */
const scrollIntoViewMock = vi.fn()
HTMLElement.prototype.scrollIntoView = scrollIntoViewMock

const SHA256 = 'abcdef0123456789'.repeat(4)

function sample(overrides: Partial<WorkbenchResponse> = {}): WorkbenchResponse {
  return {
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
      effective_date: '2026-10-01',
      expire_date: null,
      dept: '法务部',
    },
    file: {
      file_id: 7,
      file_name: 'contract.docx',
      file_type: 'DOCX',
      sha256: SHA256,
      parse_status: 'PARSED',
    },
    blocks: [
      { block_id: 1, order_index: 0, paragraph_index: 0, block_type: 'PARAGRAPH', text: '第一条 知识产权', char_start_global: 0, char_end_global: 8 },
      { block_id: 2, order_index: 1, paragraph_index: 23, block_type: 'PARAGRAPH', text: '本项目产生的知识产权归乙方所有。', char_start_global: 9, char_end_global: 25 },
    ],
    clauses: [
      {
        clause_id: 100,
        clause_no: '第一条',
        clause_type: 'IP',
        title: '知识产权',
        text: '第一条 知识产权',
        start_paragraph_index: 0,
        end_paragraph_index: 23,
        start_block_id: 1,
        end_block_id: 2,
      },
    ],
    metadata: [
      {
        field_key: 'counterparty_name',
        field_label: '相对方名称',
        field_value: '乙方公司',
        value_type: 'TEXT',
        extract_method: 'REGEX',
        source_block_id: 2,
        source_paragraph_index: 23,
      },
    ],
    risks: [
      {
        risk_id: 900,
        risk_code: 'IP_OWNER_SUPPLIER_001',
        risk_title: '知识产权归属相对方',
        dimension: '知识产权',
        risk_level: 'HIGH',
        source: 'RULE',
        reason: '成果归属供方会限制我方后续使用。',
        legal_basis: null,
        original_text: '知识产权归乙方',
        paragraph_index: 23,
        clause_id: 100,
        locator_type: 'PARAGRAPH',
        review_status: 'PENDING',
      },
    ],
    ...overrides,
  }
}

async function mountView(taskId = '42'): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({ history: createMemoryHistory(), routes })
  await router.push(`/review-tasks/${taskId}/workbench`)
  await router.isReady()

  const wrapper = mount(WorkbenchView, { global: { plugins: [router, ElementPlus] } })
  await vi.waitFor(() => expect(wrapper.find('.el-skeleton, .el-result, .el-alert, .blocks, .el-empty').exists()).toBe(true))
  return { wrapper, router }
}

beforeEach(() => {
  getWorkbenchMock.mockReset()
  getWorkbenchMock.mockResolvedValue(sample())
  scrollIntoViewMock.mockReset()
})

/** 点某张风险卡片，并等一次渲染（高亮类是在点击后由 ref 变化驱动的）。 */
async function clickRisk(wrapper: VueWrapper, riskIndex = 0): Promise<void> {
  await wrapper.findAll('.risk')[riskIndex]!.trigger('click')
  await nextTick()
}

describe('工作台：正常渲染', () => {
  it('渲染 task 信息（阶段中文名 / 进度 / 状态 / 时间）', async () => {
    const { wrapper } = await mountView()

    const text = wrapper.text()
    expect(text).toContain('审查完成') // REVIEWED 的中文名
    expect(text).toContain('pending')
    expect(wrapper.find('.el-progress').exists()).toBe(true)
    expect(text).toMatch(/\d{4}-\d{2}-\d{2} \d{2}:\d{2}/)
  })

  it('渲染 contract 信息', async () => {
    const { wrapper } = await mountView()

    const text = wrapper.text()
    expect(text).toContain('HT-2026-001')
    expect(text).toContain('设备采购合同')
    expect(text).toContain('PURCHASE')
    expect(text).toContain('某某科技')
    expect(text).toContain('乙方公司')
    expect(text).toContain('1234.50')
    expect(text).toContain('法务部')
  })

  it('渲染 file 信息，SHA-256 弱化显示但完整值仍在', async () => {
    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('contract.docx')
    expect(wrapper.text()).toContain('DOCX')
    expect(wrapper.text()).toContain('PARSED')
    // 展示的是缩写，但完整哈希放在 title 里 —— 没有改变原始数据
    const hash = wrapper.find('.workbench__hash')
    expect(hash.text()).toContain('…')
    expect(hash.attributes('title')).toBe(SHA256)
  })

  it('渲染 clause（编号 / 标题 / 类型 / 文本 / 段落区间）', async () => {
    const { wrapper } = await mountView()

    const text = wrapper.text()
    expect(text).toContain('第一条')
    expect(text).toContain('知识产权')
    expect(text).toContain('IP')
    expect(text).toContain('第 0–23 段')
  })

  it('渲染 metadata（标签 / 值 / 类型 / 提取方式 / 来源段落）', async () => {
    const { wrapper } = await mountView()

    const text = wrapper.text()
    expect(text).toContain('相对方名称')
    expect(text).toContain('乙方公司')
    expect(text).toContain('TEXT')
    expect(text).toContain('REGEX')
    expect(text).toContain('第 23 段')
  })

  it('渲染 risk 的全部要点，且 null 的 legal_basis 不显示成 "null"', async () => {
    const { wrapper } = await mountView()

    const text = wrapper.text()
    expect(text).toContain('HIGH')
    expect(text).toContain('知识产权归属相对方')
    expect(text).toContain('成果归属供方会限制我方后续使用。')
    expect(text).toContain('知识产权归乙方')
    expect(text).toContain('RULE')
    expect(text).toContain('PENDING')
    expect(text).not.toContain('null')
  })

  it('风险显示它命中的原文段落（人工对照用）', async () => {
    const { wrapper } = await mountView()

    expect(wrapper.find('.risk__fields').text()).toContain('第 23 段')
  })

  it('三种风险等级用不同的 tag 类型区分', async () => {
    const risks = ['HIGH', 'MEDIUM', 'LOW'].map((level, index) => ({
      ...sample().risks[0]!,
      risk_id: 900 + index,
      risk_level: level,
    }))
    getWorkbenchMock.mockResolvedValue(sample({ risks }))

    const { wrapper } = await mountView()

    const classes = wrapper.findAll('.risk .el-tag').map((tag) => tag.classes().join(' '))
    expect(classes.some((c) => c.includes('danger'))).toBe(true)
    expect(classes.some((c) => c.includes('warning'))).toBe(true)
    expect(classes.some((c) => c.includes('info'))).toBe(true)
  })

  it('未知的 stage 原样显示，页面不崩', async () => {
    getWorkbenchMock.mockResolvedValue(
      sample({ task: { ...sample().task, current_stage: 'SOMETHING_NEW' } }),
    )

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('SOMETHING_NEW')
  })
})

describe('工作台：loading 状态', () => {
  /**
   * 请求还没回来时页面长什么样。
   *
   * ⚠️ 这条测试**不能**用上面的 ``mountView``：那个 helper 会 ``waitFor`` 到某个
   * 状态元素出现，也就是"等请求结束"。而这里要断言的恰恰是**请求未结束**的那一瞬，
   * 所以手动 mount，并让 mock 返回一个我们攥着 resolve 不放的 Promise。
   */
  it('请求 pending 时显示骨架屏，且不提前给出空状态或错误', async () => {
    let resolveRequest!: (value: WorkbenchResponse) => void
    const pending = new Promise<WorkbenchResponse>((resolve) => {
      resolveRequest = resolve
    })
    getWorkbenchMock.mockReturnValue(pending)

    const router = createRouter({ history: createMemoryHistory(), routes })
    await router.push('/review-tasks/42/workbench')
    await router.isReady()

    const wrapper = mount(WorkbenchView, { global: { plugins: [router, ElementPlus] } })
    await nextTick()

    expect(getWorkbenchMock).toHaveBeenCalledWith(42)
    expect(wrapper.find('.el-skeleton').exists()).toBe(true)

    // "还没拿到" 与 "没有数据" / "加载失败" 是三件事，pending 时后两者都不能出现
    expect(wrapper.text()).not.toContain('暂无风险')
    expect(wrapper.text()).not.toContain('暂无原文')
    expect(wrapper.text()).not.toContain('加载审查工作台失败')
    expect(wrapper.find('.blocks').exists()).toBe(false)

    // 刷新按钮反映同一次挂起（el-button 的 loading 会带上 is-loading）
    const refresh = wrapper.findAll('button').find((b) => b.text().includes('刷新'))
    expect(refresh!.classes()).toContain('is-loading')

    // 收尾：放行请求并等渲染稳定，避免把 pending 的 Promise 留在测试进程里
    resolveRequest(sample())
    await vi.waitFor(() => expect(wrapper.find('.blocks').exists()).toBe(true))
    expect(wrapper.find('.el-skeleton').exists()).toBe(false)
  })
})

describe('工作台：原文与 data-paragraph-index', () => {
  it('渲染全部 blocks', async () => {
    const { wrapper } = await mountView()

    const blocks = wrapper.findAll('.block')
    expect(blocks).toHaveLength(2)
    expect(blocks[0]!.text()).toContain('第一条 知识产权')
    expect(blocks[1]!.text()).toContain('本项目产生的知识产权归乙方所有。')
    expect(blocks[0]!.text()).toContain('[0]')
  })

  it('每个 block 都带 data-paragraph-index（P11-7 的定位坐标）', async () => {
    const { wrapper } = await mountView()

    const indexes = wrapper.findAll('.block').map((block) => block.attributes('data-paragraph-index'))
    expect(indexes).toEqual(['0', '23'])
  })

  it('风险指向的段落号能在原文里找到同号元素', async () => {
    const { wrapper } = await mountView()
    const risk = sample().risks[0]!

    const target = wrapper.find(`[data-paragraph-index="${risk.paragraph_index}"]`)
    expect(target.exists()).toBe(true)
    expect(target.text()).toContain(risk.original_text!)
  })
})

describe('工作台：风险定位（P11-7）', () => {
  /**
   * 定位的唯一依据是 ``risk.paragraph_index`` → ``[data-paragraph-index]``。
   * 下面这些用例刻意**不**检查任何文本搜索行为 —— 因为生产代码里就没有。
   */

  it('点击风险后滚动到它命中的段落', async () => {
    const { wrapper } = await mountView()
    expect(scrollIntoViewMock).not.toHaveBeenCalled()

    await clickRisk(wrapper)

    // 风险指向 paragraph_index=23，原文里确实有这一段
    const target = wrapper.find('[data-paragraph-index="23"]')
    expect(target.exists()).toBe(true)
    expect(target.text()).toContain('本项目产生的知识产权归乙方所有。')

    expect(scrollIntoViewMock).toHaveBeenCalledTimes(1)
    expect(scrollIntoViewMock).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center' })
    // 滚的是**那一段自己**，不是某个容器或 window
    const scrolled = scrollIntoViewMock.mock.contexts[0] as unknown as HTMLElement
    expect(scrolled.getAttribute('data-paragraph-index')).toBe('23')
  })

  it('命中的段落被高亮，其它段落不受影响', async () => {
    const { wrapper } = await mountView()

    await clickRisk(wrapper)

    const blocks = wrapper.findAll('.block')
    expect(blocks[0]!.classes()).not.toContain('is-risk-target')
    expect(blocks[1]!.classes()).toContain('is-risk-target')
    expect(wrapper.findAll('.is-risk-target')).toHaveLength(1)
  })

  it('点另一条风险时高亮切换，不会同时亮两段', async () => {
    const first = sample().risks[0]! // paragraph_index = 23
    const second = { ...first, risk_id: 901, risk_title: '付款条件风险', paragraph_index: 0 }
    getWorkbenchMock.mockResolvedValue(sample({ risks: [first, second] }))

    const { wrapper } = await mountView()

    await clickRisk(wrapper, 1)
    expect(wrapper.findAll('.block')[0]!.classes()).toContain('is-risk-target')

    await clickRisk(wrapper, 0)
    expect(wrapper.findAll('.block')[1]!.classes()).toContain('is-risk-target')
    expect(wrapper.findAll('.block')[0]!.classes()).not.toContain('is-risk-target')
    expect(wrapper.findAll('.is-risk-target')).toHaveLength(1)
    expect(scrollIntoViewMock).toHaveBeenCalledTimes(2)
  })

  it('paragraph_index 为 null：不滚动、不报错、给出提示', async () => {
    getWorkbenchMock.mockResolvedValue(
      sample({ risks: [{ ...sample().risks[0]!, risk_id: 901, paragraph_index: null }] }),
    )

    const { wrapper } = await mountView()
    await clickRisk(wrapper)

    expect(scrollIntoViewMock).not.toHaveBeenCalled()
    expect(wrapper.findAll('.is-risk-target')).toHaveLength(0)
    expect(wrapper.find('.risks__hint').exists()).toBe(true)
    // 没有段落坐标 ≠ 加载失败
    expect(wrapper.text()).not.toContain('加载审查工作台失败')
  })

  it('原文里没有该段落：不滚动、不报错、给出提示', async () => {
    getWorkbenchMock.mockResolvedValue(
      sample({ risks: [{ ...sample().risks[0]!, risk_id: 901, paragraph_index: 999 }] }),
    )

    const { wrapper } = await mountView()
    expect(wrapper.find('[data-paragraph-index="999"]').exists()).toBe(false)

    await clickRisk(wrapper)

    expect(scrollIntoViewMock).not.toHaveBeenCalled()
    expect(wrapper.findAll('.is-risk-target')).toHaveLength(0)
    expect(wrapper.find('.risks__hint').text()).toContain('未找到对应的原文段落')
  })

  it('定位失败后换个能定位的风险，依然正常高亮（提示被清掉）', async () => {
    const bad = { ...sample().risks[0]!, risk_id: 901, paragraph_index: 999 }
    const good = sample().risks[0]!
    getWorkbenchMock.mockResolvedValue(sample({ risks: [bad, good] }))

    const { wrapper } = await mountView()

    await clickRisk(wrapper, 0)
    expect(wrapper.find('.risks__hint').exists()).toBe(true)

    await clickRisk(wrapper, 1)
    expect(wrapper.find('.risks__hint').exists()).toBe(false)
    expect(wrapper.findAll('.is-risk-target')).toHaveLength(1)
  })

  it('风险卡片可点击也可键盘激活（role=button + tabindex=0 + Enter）', async () => {
    const { wrapper } = await mountView()

    const risk = wrapper.find('.risk')
    expect(risk.attributes('role')).toBe('button')
    expect(risk.attributes('tabindex')).toBe('0')

    await risk.trigger('keydown.enter')
    await nextTick()

    expect(scrollIntoViewMock).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('.is-risk-target')).toHaveLength(1)
  })

  it('原文为空时点风险不抛异常', async () => {
    getWorkbenchMock.mockResolvedValue(sample({ blocks: [] }))

    const { wrapper } = await mountView()
    await clickRisk(wrapper)

    expect(scrollIntoViewMock).not.toHaveBeenCalled()
    expect(wrapper.find('.risks__hint').exists()).toBe(true)
  })
})

describe('工作台：空集合仍是成功页面', () => {
  it.each([
    ['blocks', '暂无原文'],
    ['clauses', '暂无条款'],
    ['metadata', '暂无提取到的合同信息'],
    ['risks', '暂无风险'],
  ] as const)('%s 为空时显示对应空状态', async (field, description) => {
    getWorkbenchMock.mockResolvedValue(sample({ [field]: [] }))

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain(description)
    // 空集合是**结论**，不是加载失败
    expect(wrapper.text()).not.toContain('加载审查工作台失败')
  })
})

describe('工作台：错误处理', () => {
  it('task 404 时显示"审查任务不存在"并提供返回入口', async () => {
    getWorkbenchMock.mockRejectedValue(
      new ApiError({ code: 'TASK_NOT_FOUND', message: '审查任务 42 不存在', status: 404 }),
    )

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('审查任务不存在')
    expect(wrapper.text()).toContain('返回合同列表')
    // 404 与"加载失败"是两件事
    expect(wrapper.text()).not.toContain('加载审查工作台失败')
  })

  it('其它 API 错误显示"加载审查工作台失败"，**不**显示成"暂无数据"', async () => {
    getWorkbenchMock.mockRejectedValue(
      new ApiError({ code: 'BACKEND_UNREACHABLE', message: '后端不可达', status: undefined }),
    )

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('加载审查工作台失败')
    expect(wrapper.text()).toContain('后端不可达')
    expect(wrapper.text()).not.toContain('暂无风险')
  })

  it('非 ApiError 的异常也有可读提示', async () => {
    getWorkbenchMock.mockRejectedValue(new Error('boom'))

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('加载审查工作台失败')
  })

  it('taskId 不是合法数字时不发请求，按"任务不存在"处理', async () => {
    const { wrapper } = await mountView('abc')

    expect(getWorkbenchMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('审查任务不存在')
  })
})

describe('工作台：导航与刷新', () => {
  it('点「返回合同列表」走 /contracts（不依赖 history）', async () => {
    const { wrapper, router } = await mountView()
    const push = vi.spyOn(router, 'push')

    const back = wrapper.findAll('button').find((b) => b.text().includes('返回合同列表'))
    await back!.trigger('click')

    expect(push).toHaveBeenCalledWith('/contracts')
  })

  it('点「刷新」重新取数', async () => {
    const { wrapper } = await mountView()
    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)
    expect(getWorkbenchMock).toHaveBeenCalledWith(42)

    const refresh = wrapper.findAll('button').find((b) => b.text().includes('刷新'))
    await refresh!.trigger('click')
    await vi.waitFor(() => expect(getWorkbenchMock).toHaveBeenCalledTimes(2))
  })
})

describe('工作台：数据只读', () => {
  it('渲染不会修改 API 返回的对象', async () => {
    // 深冻结：一旦组件试图往响应对象上挂展示状态（如 risk.highlighted = true），
    // 严格模式下会直接抛 TypeError。
    const frozen = Object.freeze(
      sample({
        blocks: Object.freeze(sample().blocks.map((b) => Object.freeze(b))) as never,
        clauses: Object.freeze(sample().clauses.map((c) => Object.freeze(c))) as never,
        metadata: Object.freeze(sample().metadata.map((m) => Object.freeze(m))) as never,
        risks: Object.freeze(sample().risks.map((r) => Object.freeze(r))) as never,
      }),
    ) as WorkbenchResponse
    Object.freeze(frozen.task)
    Object.freeze(frozen.contract)
    Object.freeze(frozen.file)
    getWorkbenchMock.mockResolvedValue(frozen)
    const snapshot = JSON.parse(JSON.stringify(frozen))

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('知识产权归属相对方')
    // 双重保险：冻结会让"写入"抛错，这里再逐字段比对一次，确认渲染前后完全一致
    expect(JSON.parse(JSON.stringify(frozen))).toEqual(snapshot)
  })

  it('P11-7 的定位同样不写入 API 数据（高亮状态在 ref 里，不在对象上）', async () => {
    const frozen = Object.freeze(
      sample({
        blocks: Object.freeze(sample().blocks.map((b) => Object.freeze(b))) as never,
        risks: Object.freeze(sample().risks.map((r) => Object.freeze(r))) as never,
      }),
    ) as WorkbenchResponse
    Object.freeze(frozen.task)
    Object.freeze(frozen.contract)
    Object.freeze(frozen.file)
    Object.freeze(frozen.clauses)
    Object.freeze(frozen.metadata)
    getWorkbenchMock.mockResolvedValue(frozen)
    const snapshot = JSON.parse(JSON.stringify(frozen))

    const { wrapper } = await mountView()
    await clickRisk(wrapper)

    expect(wrapper.findAll('.block')[1]!.classes()).toContain('is-risk-target')
    expect(JSON.parse(JSON.stringify(frozen))).toEqual(snapshot)
  })
})
