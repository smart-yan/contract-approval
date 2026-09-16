/**
 * 工作台的"审查中 → 出结果"轮询（P14-5-1，**组件级**）。
 *
 * 上传后跳进工作台时，任务多半还在 Agent 的后台跑（P14-4 起受理即返回 202），
 * 因此这一页必须自己把状态**问出来**。这里钉住的是页面的对外行为：
 *
 * * 处理中 → 显示等待横幅，并按节奏继续问
 * * 出结果（``current_stage=REVIEWED``）→ 横幅消失、渲染结果、**停止轮询**
 * * 被阻塞（``status=blocked``）→ 显示阻塞横幅、**停止轮询**
 * * 取数失败 → 显示错误、**停止轮询**（不许一边报错一边继续请求）
 * * 组件卸载 → 不再有任何请求
 *
 * ⚠️ 用假定时器：轮询的节奏就是被测对象，真等 1 秒既慢又测不准。
 * 相应地不能用 ``vi.waitFor``（它自己也靠定时器），改为显式推进 + ``nextTick``。
 */
import { mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import { ApiError } from '@/api/request'
import type { WorkbenchResponse } from '@/api/workbench'
import { routes } from '@/router'
import WorkbenchView from '@/views/workbench/WorkbenchView.vue'

const getWorkbenchMock = vi.fn()
vi.mock('@/api/workbench', () => ({
  getReviewTaskWorkbench: (taskId: number) => getWorkbenchMock(taskId),
}))

/** 假定时器下把微任务队列跑完（在途的请求才会落地）。 */
async function flush(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0)
}

function sample(task: Partial<WorkbenchResponse['task']>, risks: WorkbenchResponse['risks'] = []): WorkbenchResponse {
  return {
    task: {
      task_id: 42,
      status: 'pending',
      current_stage: 'UPLOADED',
      progress: 10,
      created_at: '2026-09-15T10:00:00',
      finished_at: null,
      risk_level_final: null,
      conclusion: null,
      // 默认"没被阻塞"—— 阻塞的两个用例各自覆盖自己要的形状
      block_reason_code: null,
      block_reason_msg: null,
      ...task,
    },
    contract: {
      contract_id: 1,
      contract_no: 'HT-2026-001',
      title: '设备采购合同',
      contract_type: 'PURCHASE',
      our_party: null,
      counterparty: null,
      amount: null,
      currency: null,
      sign_date: null,
      effective_date: null,
      expire_date: null,
      dept: null,
    },
    file: {
      file_id: 7,
      file_name: 'contract.docx',
      file_type: 'DOCX',
      sha256: 'a'.repeat(64),
      parse_status: 'PENDING',
    },
    blocks: [],
    clauses: [],
    metadata: [],
    risks,
  }
}

/** 处理中：图还在后台跑（status 仍是 pending，阶段还没到 REVIEWED）。 */
const PROCESSING = () => sample({ status: 'pending', current_stage: 'CLAUSED', progress: 75 })

/** 审查完成：P9-10 冻结的语义 —— **status 仍可能是 pending**，看的是阶段。 */
const REVIEWED = () =>
  sample({ status: 'pending', current_stage: 'REVIEWED', progress: 100 }, [
    {
      risk_id: 900,
      risk_code: 'IP_OWNER_SUPPLIER_001',
      risk_title: '知识产权归属相对方',
      dimension: '知识产权',
      risk_level: 'HIGH',
      source: 'RULE',
      reason: null,
      legal_basis: null,
      original_text: null,
      paragraph_index: 23,
      clause_id: null,
      locator_type: 'PARAGRAPH',
      review_status: 'PENDING',
      reviewer_id: null,
      review_comment: null,
      reviewed_at: null,
    },
  ])

/**
 * Agent 在后台跑挂了（P14-4 的 block 上报）—— 阶段停在它当时走到的位置。
 *
 * ``block_reason_*`` 是 P14-5-2 新增的透传字段：有原因时带原因，没有时为 null。
 */
const BLOCKED = (reason: string | null = '该文件类型暂不支持') =>
  sample({
    status: 'blocked',
    current_stage: 'UPLOADED',
    progress: 10,
    block_reason_code: reason === null ? null : 'UNSUPPORTED_FORMAT',
    block_reason_msg: reason,
  })

async function mountView(): Promise<VueWrapper> {
  const router = createRouter({ history: createMemoryHistory(), routes })
  await router.push('/review-tasks/42/workbench')
  await router.isReady()

  const wrapper = mount(WorkbenchView, { global: { plugins: [router, ElementPlus] } })
  await flush()
  await nextTick()
  return wrapper
}

/** 推进 ``seconds`` 秒并让视图更新（定时器回调会触发渲染）。 */
async function advance(seconds: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(seconds * 1_000)
  await nextTick()
}

beforeEach(() => {
  vi.useFakeTimers()
  getWorkbenchMock.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('工作台：审查中轮询', () => {
  it('处理中显示等待横幅与当前阶段，并按节奏继续请求', async () => {
    getWorkbenchMock.mockResolvedValue(PROCESSING())

    const wrapper = await mountView()

    expect(wrapper.text()).toContain('AI 正在审查中')
    expect(wrapper.text()).toContain('已切分条款') // CLAUSED 的中文名
    expect(wrapper.text()).toContain('进度 75%')
    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)

    await advance(1)
    expect(getWorkbenchMock).toHaveBeenCalledTimes(2)

    await advance(2)
    expect(getWorkbenchMock).toHaveBeenCalledTimes(3)

    wrapper.unmount()
  })

  it('出结果后：横幅消失、渲染风险，并**停止**轮询', async () => {
    getWorkbenchMock.mockResolvedValueOnce(PROCESSING()).mockResolvedValue(REVIEWED())

    const wrapper = await mountView()
    expect(wrapper.text()).toContain('AI 正在审查中')

    await advance(1) // 第二次取数返回终态

    expect(wrapper.text()).not.toContain('AI 正在审查中')
    expect(wrapper.text()).toContain('审查完成') // REVIEWED 的中文名
    expect(wrapper.text()).toContain('知识产权归属相对方')
    expect(getWorkbenchMock).toHaveBeenCalledTimes(2)

    // 关键：终态之后再推进多久都不该有请求
    await advance(30)
    expect(getWorkbenchMock).toHaveBeenCalledTimes(2)

    wrapper.unmount()
  })

  it('status=pending 与 current_stage=REVIEWED 并存时**不算失败**（P14-4 冻结语义）', async () => {
    getWorkbenchMock.mockResolvedValue(REVIEWED())

    const wrapper = await mountView()

    expect(wrapper.text()).not.toContain('已阻塞')
    expect(wrapper.text()).not.toContain('AI 正在审查中')
    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)

    await advance(30)
    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)

    wrapper.unmount()
  })

  it('被阻塞时显示阻塞横幅并停止轮询', async () => {
    getWorkbenchMock.mockResolvedValue(BLOCKED())

    const wrapper = await mountView()

    expect(wrapper.text()).toContain('审查任务已阻塞')
    expect(wrapper.text()).toContain('blocked') // 任务状态原样展示，不美化
    expect(wrapper.text()).not.toContain('AI 正在审查中')

    await advance(30)
    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)

    wrapper.unmount()
  })

  it('被阻塞且服务端给了原因 → 显示**真实**原因（P14-5-2）', async () => {
    getWorkbenchMock.mockResolvedValue(BLOCKED('文档已加密，无法解析'))

    const wrapper = await mountView()

    expect(wrapper.text()).toContain('审查任务已阻塞')
    expect(wrapper.text()).toContain('原因：文档已加密，无法解析')
    // 不显示那句"没有原因"的兜底话术
    expect(wrapper.text()).not.toContain('服务端未提供具体原因')

    wrapper.unmount()
  })

  it('被阻塞但没有原因 → 兜底文案，不显示 null 也不留空白（P14-5-2）', async () => {
    getWorkbenchMock.mockResolvedValue(BLOCKED(null))

    const wrapper = await mountView()

    expect(wrapper.text()).toContain('审查任务已阻塞')
    expect(wrapper.text()).toContain('审查已阻塞，服务端未提供具体原因')
    expect(wrapper.text()).not.toContain('原因：')
    expect(wrapper.text()).not.toContain('null')

    wrapper.unmount()
  })

  it('非阻塞任务不显示任何阻塞原因（两个字段为 null）', async () => {
    // 处理中与已完成两种非阻塞形态都不该出现阻塞话术
    getWorkbenchMock.mockResolvedValueOnce(PROCESSING()).mockResolvedValue(REVIEWED())

    const wrapper = await mountView()

    expect(wrapper.text()).not.toContain('已阻塞')
    expect(wrapper.text()).not.toContain('原因：')

    await advance(1)

    expect(wrapper.text()).not.toContain('已阻塞')
    expect(wrapper.text()).not.toContain('原因：')

    wrapper.unmount()
  })

  it('取数失败：显示错误并停止轮询，不会一直重试', async () => {
    getWorkbenchMock.mockRejectedValue(new ApiError({ code: 'BACKEND_UNREACHABLE', message: '后端不可达' }))

    const wrapper = await mountView()

    expect(wrapper.text()).toContain('加载审查工作台失败')
    expect(wrapper.text()).toContain('后端不可达')
    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)

    await advance(60)
    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)

    wrapper.unmount()
  })

  it('组件卸载后不再发请求（定时器不活过组件）', async () => {
    getWorkbenchMock.mockResolvedValue(PROCESSING())

    const wrapper = await mountView()
    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)

    wrapper.unmount()
    await advance(60)

    expect(getWorkbenchMock).toHaveBeenCalledTimes(1)
  })

  it('taskId 不存在时按"任务不存在"处理，且不发请求', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes })
    await router.push('/review-tasks/not-a-number/workbench')
    await router.isReady()

    const wrapper = mount(WorkbenchView, { global: { plugins: [router, ElementPlus] } })
    await flush()
    await nextTick()

    expect(wrapper.text()).toContain('审查任务不存在')
    expect(getWorkbenchMock).not.toHaveBeenCalled()

    await advance(60)
    expect(getWorkbenchMock).not.toHaveBeenCalled()

    wrapper.unmount()
  })
})
