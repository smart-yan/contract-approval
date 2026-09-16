/**
 * 工作台 → 审批回写（P16-2）的集成测试。
 *
 * 覆盖：
 * * 仅 ``phase === 'reviewed'`` 时显示写回卡片
 * * 初始状态："回写审批意见"按钮 + 点击 → loading → success
 * * FAILED：重试按钮 + 重试行为
 * * SUCCESS 后按钮 disabled，不会重新发起请求
 * * 409 ``WRITEBACK_ALREADY_SUCCESS`` → 与正常成功路径一致（不显示 error）
 * * 其它 4xx 业务错误显示在 ``workbench__writeback-error``
 * * 网络错误走 ApiError 兜底
 * * 写回结果**不**被 workbench 重新加载清掉
 *
 * 与 ``workbench-view.spec.ts`` 是同一套 mock 风格：
 * ``getReviewTaskWorkbench`` 走 vi.mock、``scrollIntoView`` 走 prototype stub。
 */

import { mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
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

const postWritebackMock = vi.fn()
vi.mock('@/api/writeback', () => ({
  postWriteback: (taskId: number) => postWritebackMock(taskId),
}))

// jsdom 没有 scrollIntoView —— 跟其它 workbench 测试同样的补桩
HTMLElement.prototype.scrollIntoView = vi.fn()

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
      effective_date: '2026-10-01',
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

/** 找到写回卡片（.workbench__writeback 在卡片内部的那个容器） */
function writebackCard(wrapper: VueWrapper): DOMWrapper<Element> | null {
  const card = wrapper.findAll('.el-card').find((c) => c.text().includes('审批回写')) ?? null
  return card
}

beforeEach(() => {
  getWorkbenchMock.mockReset()
  getWorkbenchMock.mockResolvedValue(sample())
  postWritebackMock.mockReset()
})

// --------------------------------------------------------------------------- //
// 卡片显示条件
// --------------------------------------------------------------------------- //
describe('工作台：审批回写卡片显示条件', () => {
  it('phase=reviewed 时显示写回卡片', async () => {
    const { wrapper } = await mountView()

    expect(writebackCard(wrapper)).not.toBeNull()
  })

  it('phase=processing 时不显示写回卡片', async () => {
    getWorkbenchMock.mockResolvedValue(
      sample({ task: { ...sample().task, current_stage: 'CLAUSED' } }),
    )

    const { wrapper } = await mountView()

    expect(writebackCard(wrapper)).toBeNull()
  })

  it('phase=blocked 时不显示写回卡片', async () => {
    getWorkbenchMock.mockResolvedValue(
      sample({
        task: {
          ...sample().task,
          status: 'blocked',
          current_stage: 'CLAUSED',
          block_reason_code: 'OCR_FAILED',
          block_reason_msg: 'OCR 失败',
        },
      }),
    )

    const { wrapper } = await mountView()

    expect(writebackCard(wrapper)).toBeNull()
  })

  it('初次进入时（默认 sample=reviewed）显示「回写审批意见」按钮', async () => {
    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    expect(card.text()).toContain('回写审批意见')
    expect(card.text()).not.toContain('已成功回写')
    expect(card.text()).not.toContain('回写失败')
  })
})

// --------------------------------------------------------------------------- //
// 成功路径
// --------------------------------------------------------------------------- //
describe('工作台：写回成功', () => {
  it('点回写 → loading → success → 显示 external_comment_id 与 attempt', async () => {
    postWritebackMock.mockResolvedValue({
      record_id: 1,
      task_id: 42,
      approval_instance_id: 7,
      status: 'success',
      attempt: 1,
      external_comment_id: 'MOCK-CMT-100',
      error_msg: null,
      finished_at: '2026-09-16T10:00:00',
      posted: true,
    })

    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    const button = card.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await button.trigger('click')
    await nextTick()
    await vi.waitFor(() => expect(postWritebackMock).toHaveBeenCalledTimes(1))

    // 调用时只传 taskId，不传任何 body 字段
    expect(postWritebackMock).toHaveBeenCalledWith(42)

    // 等 loading 结束、结果展示出来
    await vi.waitFor(() => {
      const updated = writebackCard(wrapper)!
      return updated.text().includes('已成功回写')
    })

    const updatedCard = writebackCard(wrapper)!
    expect(updatedCard.text()).toContain('MOCK-CMT-100')
    expect(updatedCard.text()).toContain('第 1 次')
    expect(postWritebackMock).toHaveBeenCalledTimes(1)
  })

  it('posted=false 时提示"本次未重复发送"，而不是"成功回写"', async () => {
    postWritebackMock.mockResolvedValue({
      record_id: 1,
      task_id: 42,
      approval_instance_id: 7,
      status: 'success',
      attempt: 2,
      external_comment_id: 'MOCK-CMT-100',
      error_msg: null,
      finished_at: '2026-09-16T10:00:00',
      posted: false, // 外部已有 → 认回来
    })

    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    const button = card.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await button.trigger('click')

    await vi.waitFor(() => expect(writebackCard(wrapper)!.text()).toContain('未重复发送'))
    expect(writebackCard(wrapper)!.text()).not.toContain('已成功回写到审批单')
  })

  it('SUCCESS 后"已成功回写"按钮 disabled，不允许重复点击', async () => {
    postWritebackMock.mockResolvedValue({
      record_id: 1,
      task_id: 42,
      approval_instance_id: 7,
      status: 'success',
      attempt: 1,
      external_comment_id: 'MOCK-CMT-100',
      error_msg: null,
      finished_at: '2026-09-16T10:00:00',
      posted: true,
    })

    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    const submitBtn = card.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await submitBtn.trigger('click')

    await vi.waitFor(() => expect(writebackCard(wrapper)!.text()).toContain('已成功回写'))

    // SUCCESS 之后卡片里的按钮应当 disabled
    const afterCard = writebackCard(wrapper)!
    const disabledBtn = afterCard.findAll('button').find((b) => b.text().includes('已成功回写'))!
    expect(disabledBtn.exists()).toBe(true)
    expect(disabledBtn.attributes('disabled')).toBeDefined()

    // 即使用户触发（disabled 也不会真的发出请求；这里 postWritebackMock 没变）
    expect(postWritebackMock).toHaveBeenCalledTimes(1)
  })
})

// --------------------------------------------------------------------------- //
// FAILED 路径
// --------------------------------------------------------------------------- //
describe('工作台：写回失败', () => {
  it('HTTP 200 + status=failed 时显示 error_msg 与「重试写回」', async () => {
    postWritebackMock.mockResolvedValue({
      record_id: 1,
      task_id: 42,
      approval_instance_id: 7,
      status: 'failed',
      attempt: 1,
      external_comment_id: null,
      error_msg: '审批系统不可达',
      finished_at: '2026-09-16T10:01:00',
      posted: true,
    })

    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    const button = card.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await button.trigger('click')

    await vi.waitFor(() => expect(writebackCard(wrapper)!.text()).toContain('回写失败'))

    const afterCard = writebackCard(wrapper)!
    expect(afterCard.text()).toContain('审批系统不可达')
    expect(afterCard.text()).toContain('重试写回')

    // 不显示 success 提示
    expect(afterCard.text()).not.toContain('已成功回写')
  })

  it('点「重试写回」重新调用 postWriteback（同一个 task_id，不重新生成）', async () => {
    postWritebackMock
      .mockResolvedValueOnce({
        record_id: 1,
        task_id: 42,
        approval_instance_id: 7,
        status: 'failed',
        attempt: 1,
        external_comment_id: null,
        error_msg: '审批系统不可达',
        finished_at: '2026-09-16T10:01:00',
        posted: true,
      })
      .mockResolvedValueOnce({
        record_id: 1,
        task_id: 42,
        approval_instance_id: 7,
        status: 'success',
        attempt: 2,
        external_comment_id: 'MOCK-CMT-200',
        error_msg: null,
        finished_at: '2026-09-16T10:02:00',
        posted: true,
      })

    const { wrapper } = await mountView()

    // 第一次
    const card1 = writebackCard(wrapper)!
    const btn1 = card1.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await btn1.trigger('click')

    await vi.waitFor(() => expect(writebackCard(wrapper)!.text()).toContain('重试写回'))

    // 第二次（重试）
    const card2 = writebackCard(wrapper)!
    const retryBtn = card2.findAll('button').find((b) => b.text().includes('重试写回'))!
    await retryBtn.trigger('click')

    await vi.waitFor(() => expect(writebackCard(wrapper)!.text()).toContain('已成功回写'))

    expect(postWritebackMock).toHaveBeenCalledTimes(2)
    // 两次都传同一个 task_id —— Frontend 不重新生成
    expect(postWritebackMock.mock.calls[0]![0]).toBe(42)
    expect(postWritebackMock.mock.calls[1]![0]).toBe(42)
  })
})

// --------------------------------------------------------------------------- //
// 409 处理
// --------------------------------------------------------------------------- //
describe('工作台：409 WRITEBACK_ALREADY_SUCCESS', () => {
  it('409 不显示成错误，而是当作 success 处理', async () => {
    postWritebackMock.mockRejectedValue(
      new ApiError({
        code: 'WRITEBACK_ALREADY_SUCCESS',
        message: '该意见已成功回写，无需重复提交',
        status: 409,
      }),
    )

    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    const button = card.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await button.trigger('click')

    await vi.waitFor(() => expect(writebackCard(wrapper)!.text()).toContain('已成功回写'))

    // 没有业务错误 alert
    expect(wrapper.find('.workbench__writeback-error').exists()).toBe(false)
    // 不自动 retry
    expect(postWritebackMock).toHaveBeenCalledTimes(1)
  })
})

// --------------------------------------------------------------------------- //
// 其它业务错误
// --------------------------------------------------------------------------- //
describe('工作台：其它业务错误', () => {
  it('409 WRITEBACK_NOT_READY → 显示明确的业务文案', async () => {
    postWritebackMock.mockRejectedValue(
      new ApiError({
        code: 'WRITEBACK_NOT_READY',
        message: '任务尚未审查完成',
        status: 409,
      }),
    )

    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    const button = card.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await button.trigger('click')

    await vi.waitFor(() => expect(wrapper.find('.workbench__writeback-error').exists()).toBe(true))
    expect(wrapper.find('.workbench__writeback-error').text()).toContain('尚未审查完成')
  })

  it('409 WRITEBACK_APPROVAL_NOT_READY → 显示明确的业务文案', async () => {
    postWritebackMock.mockRejectedValue(
      new ApiError({
        code: 'WRITEBACK_APPROVAL_NOT_READY',
        message: '该合同尚未关联审批单',
        status: 409,
      }),
    )

    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    const button = card.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await button.trigger('click')

    await vi.waitFor(() => expect(wrapper.find('.workbench__writeback-error').exists()).toBe(true))
    expect(wrapper.find('.workbench__writeback-error').text()).toContain('未关联审批单')
  })

  it('非 ApiError 的网络异常也走兜底文案', async () => {
    postWritebackMock.mockRejectedValue(new Error('boom'))

    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    const button = card.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await button.trigger('click')

    await vi.waitFor(() => expect(wrapper.find('.workbench__writeback-error').exists()).toBe(true))
    expect(wrapper.find('.workbench__writeback-error').text()).toContain('请稍后重试')
  })
})

// --------------------------------------------------------------------------- //
// 持久性：写回结果不因刷新被清掉
// --------------------------------------------------------------------------- //
describe('工作台：写回结果跨刷新保留', () => {
  it('成功写回后点"刷新"按钮，写回结果仍显示 SUCCESS', async () => {
    postWritebackMock.mockResolvedValue({
      record_id: 1,
      task_id: 42,
      approval_instance_id: 7,
      status: 'success',
      attempt: 1,
      external_comment_id: 'MOCK-CMT-100',
      error_msg: null,
      finished_at: '2026-09-16T10:00:00',
      posted: true,
    })

    const { wrapper } = await mountView()

    const card = writebackCard(wrapper)!
    const submitBtn = card.findAll('button').find((b) => b.text().includes('回写审批意见'))!
    await submitBtn.trigger('click')

    await vi.waitFor(() => expect(writebackCard(wrapper)!.text()).toContain('已成功回写'))

    // 点"刷新"（注意不要点卡片里的 disabled 按钮）
    const refreshBtn = wrapper.findAll('button').find((b) => b.text().trim() === '刷新')!
    await refreshBtn.trigger('click')

    // 等第二次 getReviewTaskWorkbench 完成（轮询已经在 isSettled 时就停了，但点击刷新会再发一次）
    await vi.waitFor(() => expect(getWorkbenchMock).toHaveBeenCalledTimes(2))

    // 写回卡片仍然显示 SUCCESS —— 不被 onData 清掉
    expect(writebackCard(wrapper)!.text()).toContain('已成功回写')
    expect(writebackCard(wrapper)!.text()).toContain('MOCK-CMT-100')
  })
})