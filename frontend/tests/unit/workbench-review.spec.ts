/**
 * 工作台的人工复核 UI（P13-3）。
 *
 * 关注的只有一件事：**"AI 说了什么"与"法务怎么看"在卡片上是两件事**。
 * 因此样本里**必须有已复核的风险** —— 全部用 PENDING 的话，"已复核的风险还能再改"
 * 这条最关键的保证就完全没被测到。
 */
import { mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElSelect } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

import { ApiError } from '@/api/request'
import type { WorkbenchResponse, WorkbenchRisk } from '@/api/workbench'
import { RISK_REVIEW_DECISIONS } from '@/constants/review'
import { routes } from '@/router'
import WorkbenchView from '@/views/workbench/WorkbenchView.vue'

const getWorkbenchMock = vi.fn()
vi.mock('@/api/workbench', () => ({
  getReviewTaskWorkbench: (taskId: number) => getWorkbenchMock(taskId),
}))

const reviewMock = vi.fn()
vi.mock('@/api/risk-review', () => ({
  reviewRiskItem: (taskId: number, riskId: number, payload: unknown) =>
    reviewMock(taskId, riskId, payload),
}))

/**
 * jsdom 没有实现 ``Element.prototype.scrollIntoView``，不补桩调用就会抛错。
 * 复核表单与"点击定位"共用同一张卡片，因此这里要靠它验证**冒泡有没有被拦住**。
 */
const scrollIntoViewMock = vi.fn()
HTMLElement.prototype.scrollIntoView = scrollIntoViewMock

const TASK_ID = 42


function makeRisk(overrides: Partial<WorkbenchRisk> & { risk_id: number }): WorkbenchRisk {
  return {
    risk_code: 'IP_OWNER_SUPPLIER_001',
    risk_title: '知识产权归属相对方',
    dimension: '知识产权',
    risk_level: 'HIGH',
    source: 'RULE',
    reason: '成果归属供方会限制我方后续使用。',
    legal_basis: '《民法典》第八百四十七条',
    original_text: '知识产权归乙方',
    paragraph_index: 23,
    clause_id: 100,
    locator_type: 'PARAGRAPH',
    review_status: 'PENDING',
    reviewer_id: null,
    review_comment: null,
    reviewed_at: null,
    ...overrides,
  }
}

/**
 * 四条风险，覆盖**全部四种复核状态**。
 *
 * 索引固定，用例直接按位置取：0=PENDING 1=CONFIRMED 2=REJECTED 3=MODIFIED。
 */
const RISKS: WorkbenchRisk[] = [
  makeRisk({ risk_id: 900, risk_title: '待处理的风险' }),
  makeRisk({
    risk_id: 901,
    risk_title: '已确认的风险',
    review_status: 'CONFIRMED',
    review_comment: '已与业务确认',
    reviewed_at: '2026-09-15T01:00:00.000',
  }),
  makeRisk({
    risk_id: 902,
    risk_title: '已驳回的风险',
    review_status: 'REJECTED',
    review_comment: '属于正常业务条款',
    reviewed_at: '2026-09-15T02:00:00.000',
  }),
  makeRisk({
    risk_id: 903,
    risk_title: '已修改的风险',
    risk_level: 'LOW',
    review_status: 'MODIFIED',
    review_comment: '等级下调',
    reviewed_at: '2026-09-16T08:12:34.567',
  }),
]

function sample(): WorkbenchResponse {
  return {
    task: {
      task_id: TASK_ID,
      status: 'pending',
      current_stage: 'REVIEWED',
      progress: 100,
      created_at: '2026-09-15T10:00:00',
      finished_at: null,
      risk_level_final: null,
      conclusion: null,
      // P14-5-2：阻塞原因。**没被阻塞的任务两个都是 null**（键存在，值为空）
      block_reason_code: null,
      block_reason_msg: null,
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
    // 留一段带 paragraph_index=23 的原文：复核表单与"点击定位"共用同一张卡片，
    // 要靠它验证"表单拦住了冒泡、卡片本体照常定位"
    blocks: [
      {
        block_id: 1,
        order_index: 0,
        paragraph_index: 23,
        block_type: 'PARAGRAPH',
        text: '本项目产生的知识产权归乙方所有。',
        char_start_global: 0,
        char_end_global: 17,
      },
    ],
    clauses: [],
    metadata: [],
    risks: RISKS,
  }
}

const SETTLED = {
  risk_id: 900,
  task_id: TASK_ID,
  risk_level: 'HIGH',
  review_status: 'CONFIRMED',
  review_comment: '确认存在风险',
  reviewer_id: null,
  reviewed_at: '2026-09-16T09:30:00.000',
}

async function mountView(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({ history: createMemoryHistory(), routes })
  await router.push(`/review-tasks/${TASK_ID}/workbench`)
  await router.isReady()

  const wrapper = mount(WorkbenchView, { global: { plugins: [router, ElementPlus] } })
  await vi.waitFor(() => expect(wrapper.findAll('.risk')).toHaveLength(RISKS.length))
  return { wrapper, router }
}

/** 第 index 张风险卡片内的某个元素。 */
function card(wrapper: VueWrapper, index: number) {
  return wrapper.findAll('.risk')[index]!
}

function statusSelect(wrapper: VueWrapper, index: number) {
  return card(wrapper, index).find('.review__status').findComponent(ElSelect)
}

function saveButton(wrapper: VueWrapper, index: number) {
  return card(wrapper, index)
    .findAll('button')
    .find((button) => button.text().includes('保存复核'))!
}

/** 选中复核结论（el-select 不是原生 select，直接驱动它的 v-model）。 */
async function chooseStatus(wrapper: VueWrapper, index: number, status: string): Promise<void> {
  statusSelect(wrapper, index).vm.$emit('update:modelValue', status)
  await nextTick()
}

async function chooseLevel(wrapper: VueWrapper, index: number, level: string): Promise<void> {
  card(wrapper, index).find('.review__level').findComponent(ElSelect).vm.$emit('update:modelValue', level)
  await nextTick()
}

async function typeComment(wrapper: VueWrapper, index: number, text: string): Promise<void> {
  await card(wrapper, index).find('textarea').setValue(text)
}

beforeEach(() => {
  getWorkbenchMock.mockReset()
  getWorkbenchMock.mockResolvedValue(sample())
  reviewMock.mockReset()
  reviewMock.mockResolvedValue(SETTLED)
  scrollIntoViewMock.mockReset()
})

// --------------------------------------------------------------------------- #
// 1-4：状态显示
// --------------------------------------------------------------------------- #
describe('复核状态显示', () => {
  it.each([
    [0, '待复核'],
    [1, '已确认'],
    [2, '已驳回'],
    [3, '已修改'],
  ])('第 %i 张卡片显示「%s」', async (index, label) => {
    const { wrapper } = await mountView()

    expect(card(wrapper, index as number).text()).toContain(label)
  })

  it('原始状态码一并显示，便于排查', async () => {
    const { wrapper } = await mountView()

    expect(card(wrapper, 1).text()).toContain('CONFIRMED')
    expect(card(wrapper, 3).text()).toContain('MODIFIED')
  })

  it('未复核的卡片不显示复核时间，已复核的显示', async () => {
    const { wrapper } = await mountView()

    expect(card(wrapper, 0).text()).toContain('尚未复核')
    expect(card(wrapper, 1).text()).toContain('复核于')
  })

  it('不渲染任何编造的复核人 —— reviewer_id 恒为 null', async () => {
    const { wrapper } = await mountView()

    for (const text of ['未知用户', '系统用户', '默认复核人', '当前用户']) {
      expect(wrapper.text()).not.toContain(text)
    }
  })
})

// --------------------------------------------------------------------------- #
// 5-11：表单
// --------------------------------------------------------------------------- #
describe('复核表单', () => {
  it('可选结论只有三种，**不含 PENDING**', () => {
    // 选项由这个常量驱动；断言常量比断言 DOM 更直接（el-select 的下拉是 teleport 出去的）
    expect([...RISK_REVIEW_DECISIONS]).toEqual(['CONFIRMED', 'REJECTED', 'MODIFIED'])
    expect(RISK_REVIEW_DECISIONS).not.toContain('PENDING')
  })

  it('待复核的卡片初始没有预选结论（PENDING 不是可选项，不能预选它）', async () => {
    const { wrapper } = await mountView()

    expect(statusSelect(wrapper, 0).props('modelValue')).toBe('')
  })

  it('已复核的卡片预选它当前的结论', async () => {
    const { wrapper } = await mountView()

    expect(statusSelect(wrapper, 1).props('modelValue')).toBe('CONFIRMED')
    expect(statusSelect(wrapper, 2).props('modelValue')).toBe('REJECTED')
    expect(statusSelect(wrapper, 3).props('modelValue')).toBe('MODIFIED')
  })

  it('**已复核的风险仍然可以再改**（不是一复核就锁死）', async () => {
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 1, 'MODIFIED')

    expect(statusSelect(wrapper, 1).props('modelValue')).toBe('MODIFIED')
    expect(saveButton(wrapper, 1).attributes('disabled')).toBeUndefined()
  })

  it('复核意见回填旧值；未复核的为空', async () => {
    const { wrapper } = await mountView()

    expect((card(wrapper, 3).find('textarea').element as HTMLTextAreaElement).value).toBe('等级下调')
    expect((card(wrapper, 0).find('textarea').element as HTMLTextAreaElement).value).toBe('')
  })

  it('只有 MODIFIED 显示人工等级控件', async () => {
    const { wrapper } = await mountView()

    expect(card(wrapper, 3).find('.review__level').exists()).toBe(true)
    expect(card(wrapper, 1).find('.review__level').exists()).toBe(false)
    expect(card(wrapper, 2).find('.review__level').exists()).toBe(false)
    expect(card(wrapper, 0).find('.review__level').exists()).toBe(false)
  })

  it('选成 MODIFIED 后等级控件出现，换成别的结论又消失', async () => {
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'MODIFIED')
    expect(card(wrapper, 0).find('.review__level').exists()).toBe(true)

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    expect(card(wrapper, 0).find('.review__level').exists()).toBe(false)
  })

  it('没有选结论时保存按钮是禁用的', async () => {
    const { wrapper } = await mountView()

    expect(saveButton(wrapper, 0).attributes('disabled')).toBeDefined()
  })

  it('AI 事实字段仍然只读 —— 没有可编辑控件，也没有被改过', async () => {
    const { wrapper } = await mountView()
    const text = card(wrapper, 0).text()

    expect(text).toContain('待处理的风险')
    expect(text).toContain('HIGH')
    expect(text).toContain('成果归属供方会限制我方后续使用。')
    expect(text).toContain('《民法典》第八百四十七条')
    expect(text).toContain('知识产权归乙方')
    expect(text).toContain('第 23 段')
    // AI 事实所在的每一行里都没有输入控件
    for (const dd of card(wrapper, 0).findAll('.risk__fields dd')) {
      expect(dd.findAll('input, textarea, select')).toHaveLength(0)
    }
    // 卡片上**所有**可编辑控件都落在复核区之内（不多一个，也不少一个）
    const allEditable = card(wrapper, 0).findAll('input, textarea')
    const reviewEditable = card(wrapper, 0).find('.review').findAll('input, textarea')
    expect(allEditable.length).toBe(reviewEditable.length)
    expect(allEditable.length).toBeGreaterThan(0)
  })
})

// --------------------------------------------------------------------------- #
// 12-16：请求
// --------------------------------------------------------------------------- #
describe('保存请求', () => {
  it('CONFIRMED：只发 review_status 与 review_comment', async () => {
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await typeComment(wrapper, 0, '确认存在风险')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(reviewMock).toHaveBeenCalledTimes(1))

    const [taskId, riskId, payload] = reviewMock.mock.calls[0]!
    expect(taskId).toBe(TASK_ID)
    expect(riskId).toBe(900)
    expect(payload).toEqual({ review_status: 'CONFIRMED', review_comment: '确认存在风险' })
  })

  it('REJECTED：同样不带 risk_level（带上会被后端 422 拒绝）', async () => {
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'REJECTED')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(reviewMock).toHaveBeenCalledTimes(1))

    const payload = reviewMock.mock.calls[0]![2]
    expect(payload.review_status).toBe('REJECTED')
    expect(payload).not.toHaveProperty('risk_level')
    // 意见为空时显式传 null（服务端是整体赋值，省略等价于清空）
    expect(payload.review_comment).toBeNull()
  })

  it('MODIFIED：带上人工风险等级', async () => {
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'MODIFIED')
    await chooseLevel(wrapper, 0, 'LOW')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(reviewMock).toHaveBeenCalledTimes(1))

    expect(reviewMock.mock.calls[0]![2]).toEqual({
      review_status: 'MODIFIED',
      review_comment: null,
      risk_level: 'LOW',
    })
  })

  it('**请求体只有这三个字段** —— 不发服务端字段，也不发 AI 事实字段', async () => {
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'MODIFIED')
    await chooseLevel(wrapper, 0, 'MEDIUM')
    await typeComment(wrapper, 0, '降一级')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(reviewMock).toHaveBeenCalledTimes(1))

    const payload = reviewMock.mock.calls[0]![2] as Record<string, unknown>
    expect(Object.keys(payload).sort()).toEqual(['review_comment', 'review_status', 'risk_level'])

    for (const forbidden of [
      'reviewer_id',
      'reviewed_at',
      'task_id',
      'risk_id',
      'risk_title',
      'reason',
      'legal_basis',
      'original_text',
      'paragraph_index',
      'clause_id',
      'dimension',
      'source',
    ]) {
      expect(payload).not.toHaveProperty(forbidden)
    }
  })

  it('task_id 与 risk_id 走 URL，不放进请求体', async () => {
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 2, 'CONFIRMED')
    await saveButton(wrapper, 2).trigger('click')
    await vi.waitFor(() => expect(reviewMock).toHaveBeenCalledTimes(1))

    expect(reviewMock.mock.calls[0]![0]).toBe(TASK_ID)
    expect(reviewMock.mock.calls[0]![1]).toBe(902)
    expect(reviewMock.mock.calls[0]![2]).not.toHaveProperty('task_id')
  })

  it('**在复核表单里操作不会触发卡片定位**（@click.stop 拦住了冒泡）', async () => {
    // 整张卡片是"点击定位原文"的 role=button。表单不拦住冒泡的话，
    // 点一下下拉框就会把原文滚走 —— 用户在填复核意见，页面却在背后跳。
    const { wrapper } = await mountView()
    expect(scrollIntoViewMock).not.toHaveBeenCalled()

    await card(wrapper, 0).find('textarea').trigger('click')
    await card(wrapper, 0).find('.review__status').trigger('click')
    await saveButton(wrapper, 0).trigger('click')

    expect(scrollIntoViewMock).not.toHaveBeenCalled()
    expect(wrapper.findAll('.is-risk-target')).toHaveLength(0)
  })

  it('点表单外面（卡片本体）仍然照常定位 —— 拦住冒泡没有把功能一起关掉', async () => {
    const { wrapper } = await mountView()

    await card(wrapper, 0).find('.risk__header').trigger('click')
    await nextTick()

    expect(scrollIntoViewMock).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('.is-risk-target')).toHaveLength(1)
  })
})

// --------------------------------------------------------------------------- #
// 17-22：保存成功
// --------------------------------------------------------------------------- #
describe('保存成功', () => {
  it('**不重新拉整个工作台**，只用返回值更新那一条', async () => {
    const { wrapper } = await mountView()
    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(reviewMock).toHaveBeenCalledTimes(1))

    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)
  })

  it('卡片状态、复核时间、意见都就地更新，reviewer_id 仍是 null', async () => {
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await typeComment(wrapper, 0, '确认存在风险')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(card(wrapper, 0).text()).toContain('已确认'))

    const text = card(wrapper, 0).text()
    expect(text).toContain('复核于')
    expect(text).not.toContain('尚未复核')
    expect((card(wrapper, 0).find('textarea').element as HTMLTextAreaElement).value).toBe(
      '确认存在风险',
    )
    expect(text).not.toContain('未知用户')
  })

  it('MODIFIED 保存后，卡片的等级与等级控件都跟着变', async () => {
    reviewMock.mockResolvedValue({ ...SETTLED, review_status: 'MODIFIED', risk_level: 'LOW' })
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'MODIFIED')
    await chooseLevel(wrapper, 0, 'LOW')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(card(wrapper, 0).text()).toContain('已修改'))

    expect(card(wrapper, 0).find('.risk__header').text()).toContain('LOW')
    expect(card(wrapper, 0).find('.review__level').findComponent(ElSelect).props('modelValue')).toBe(
      'LOW',
    )
  })

  it('保存后 AI 事实字段一个字都没变', async () => {
    const { wrapper } = await mountView()
    const before = card(wrapper, 0).text()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(card(wrapper, 0).text()).toContain('已确认'))

    const after = card(wrapper, 0).text()
    for (const fact of [
      '待处理的风险',
      '成果归属供方会限制我方后续使用。',
      '《民法典》第八百四十七条',
      '知识产权归乙方',
      '第 23 段',
    ]) {
      expect(before).toContain(fact)
      expect(after).toContain(fact)
    }
  })

  it('保存成功后错误提示消失', async () => {
    reviewMock.mockRejectedValueOnce(
      new ApiError({ code: 'RISK_NOT_FOUND', message: '风险项不存在', status: 404 }),
    )
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(card(wrapper, 0).find('.review__error').exists()).toBe(true))

    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(card(wrapper, 0).find('.review__error').exists()).toBe(false))
  })
})

// --------------------------------------------------------------------------- #
// 23-26：失败
// --------------------------------------------------------------------------- #
describe('保存失败', () => {
  it('409：任务未审完时给出明确提示，不静默失败', async () => {
    reviewMock.mockRejectedValue(
      new ApiError({
        code: 'RISK_REVIEW_NOT_READY',
        message: '任务 42 的当前阶段是 CLAUSED，尚未完成风险审查',
        status: 409,
      }),
    )
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')

    await vi.waitFor(() =>
      expect(card(wrapper, 0).find('.review__error').text()).toContain('任务尚未完成风险审查'),
    )
  })

  it('404：提示风险不存在或不属于当前任务', async () => {
    reviewMock.mockRejectedValue(
      new ApiError({ code: 'RISK_NOT_FOUND', message: '任务 42 下不存在风险项 900', status: 404 }),
    )
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')

    await vi.waitFor(() =>
      expect(card(wrapper, 0).find('.review__error').text()).toContain('不属于当前任务'),
    )
  })

  it('422：按状态码判定，**不**被误报成网络错误', async () => {
    // 这是真实形状：后端的请求体校验走 FastAPI 默认处理器，响应体是 {detail:[...]}，
    // 没有 code 字段 —— 拦截器会把它归成 NETWORK_ERROR
    reviewMock.mockRejectedValue(
      new ApiError({
        code: 'NETWORK_ERROR',
        message: 'Request failed with status code 422',
        status: 422,
      }),
    )
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')

    await vi.waitFor(() => {
      const text = card(wrapper, 0).find('.review__error').text()
      expect(text).toContain('不合法')
      expect(text).not.toContain('网络')
    })
  })

  it('未知异常也有可读提示', async () => {
    reviewMock.mockRejectedValue(new Error('boom'))
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')

    await vi.waitFor(() =>
      expect(card(wrapper, 0).find('.review__error').text()).toContain('复核失败'),
    )
  })

  it('**失败后保留用户输入**', async () => {
    reviewMock.mockRejectedValue(
      new ApiError({ code: 'RISK_NOT_FOUND', message: '风险项不存在', status: 404 }),
    )
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 1, 'MODIFIED')
    await chooseLevel(wrapper, 1, 'LOW')
    await typeComment(wrapper, 1, '保留我这一句')
    await saveButton(wrapper, 1).trigger('click')
    await vi.waitFor(() => expect(card(wrapper, 1).find('.review__error').exists()).toBe(true))

    expect(statusSelect(wrapper, 1).props('modelValue')).toBe('MODIFIED')
    expect(
      card(wrapper, 1).find('.review__level').findComponent(ElSelect).props('modelValue'),
    ).toBe('LOW')
    expect((card(wrapper, 1).find('textarea').element as HTMLTextAreaElement).value).toBe(
      '保留我这一句',
    )
  })

  it('一条失败不影响其它卡片', async () => {
    reviewMock.mockRejectedValueOnce(
      new ApiError({ code: 'RISK_NOT_FOUND', message: '风险项不存在', status: 404 }),
    )
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(card(wrapper, 0).find('.review__error').exists()).toBe(true))

    expect(card(wrapper, 3).find('.review__error').exists()).toBe(false)
    expect(card(wrapper, 3).text()).toContain('已修改')
  })
})

// --------------------------------------------------------------------------- #
// 27-28：并发
// --------------------------------------------------------------------------- #
describe('保存中的按钮状态', () => {
  it('保存期间按钮 disabled，且呈 loading', async () => {
    let resolveRequest!: (value: unknown) => void
    reviewMock.mockReturnValue(new Promise((resolve) => (resolveRequest = resolve)))
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')
    await nextTick()

    const button = saveButton(wrapper, 0)
    expect(button.attributes('disabled')).toBeDefined()
    expect(button.classes().join(' ')).toContain('is-loading')

    resolveRequest(SETTLED)
    await vi.waitFor(() => expect(saveButton(wrapper, 0).attributes('disabled')).toBeUndefined())
  })

  it('保存期间再点一次不会发出第二个请求', async () => {
    let resolveRequest!: (value: unknown) => void
    reviewMock.mockReturnValue(new Promise((resolve) => (resolveRequest = resolve)))
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')
    await nextTick()
    await saveButton(wrapper, 0).trigger('click')
    await nextTick()

    expect(reviewMock).toHaveBeenCalledTimes(1)

    resolveRequest(SETTLED)
    await vi.waitFor(() => expect(saveButton(wrapper, 0).attributes('disabled')).toBeUndefined())
  })

  it('请求完成后卡片恢复可操作', async () => {
    const { wrapper } = await mountView()

    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(reviewMock).toHaveBeenCalledTimes(1))

    // 已保存的结论成为新的起点，按钮重新可用
    expect(saveButton(wrapper, 0).attributes('disabled')).toBeUndefined()
    expect(statusSelect(wrapper, 0).props('modelValue')).toBe('CONFIRMED')
  })
})

// --------------------------------------------------------------------------- #
// 数据只读（P11-6 的原则在 P13-3 之后仍然成立）
// --------------------------------------------------------------------------- #
describe('数据只读', () => {
  it('保存复核不会修改 API 返回的那份快照', async () => {
    const data = sample()
    const snapshot = JSON.parse(JSON.stringify(data))
    getWorkbenchMock.mockResolvedValue(data)

    const { wrapper } = await mountView()
    await chooseStatus(wrapper, 0, 'CONFIRMED')
    await typeComment(wrapper, 0, '确认')
    await saveButton(wrapper, 0).trigger('click')
    await vi.waitFor(() => expect(card(wrapper, 0).text()).toContain('已确认'))

    // 复核结果落在 savedReviews 里，**没有**写回 risk 对象
    expect(JSON.parse(JSON.stringify(data))).toEqual(snapshot)
    expect(data.risks[0]!.review_status).toBe('PENDING')
  })
})
