import { mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import { ApiError } from '@/api/request'
import { routes } from '@/router'
import ContractListView from '@/views/contracts/ContractListView.vue'
import type { ContractListItem } from '@/api/contracts'

// 页面挂载时会调 GET /api/v1/contracts。单元测试里不真的发请求，
// 把整个 contracts 模块替换成可控的替身。
const getContractsMock = vi.fn()
vi.mock('@/api/contracts', () => ({
  getContracts: () => getContractsMock(),
}))

function item(overrides: Partial<ContractListItem> = {}): ContractListItem {
  return {
    contract_id: 1,
    contract_no: 'HT-2026-001',
    title: '设备采购合同',
    contract_type: 'PURCHASE',
    created_at: '2026-09-15T10:00:00',
    latest_task: { task_id: 7, status: 'pending', current_stage: 'REVIEWED', progress: 100 },
    ...overrides,
  }
}

async function mountView(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({ history: createMemoryHistory(), routes })
  await router.push('/contracts')
  await router.isReady()

  const wrapper = mount(ContractListView, {
    global: { plugins: [router, ElementPlus] },
  })
  // 等 onMounted 里的异步加载跑完
  await vi.waitFor(() => expect(wrapper.find('.el-table, .el-empty').exists()).toBe(true))
  return { wrapper, router }
}

beforeEach(() => {
  getContractsMock.mockReset()
  getContractsMock.mockResolvedValue([item()])
})

describe('合同列表页', () => {
  it('渲染合同编号 / 名称 / 类型 / 创建时间', async () => {
    const { wrapper } = await mountView()

    const text = wrapper.text()
    expect(text).toContain('HT-2026-001')
    expect(text).toContain('设备采购合同')
    expect(text).toContain('PURCHASE')
    // 创建时间按本地时区格式化（不断言具体字面量 —— 那会依赖运行机器的时区）
    expect(text).toMatch(/\d{4}-\d{2}-\d{2} \d{2}:\d{2}/)
  })

  it('有任务时显示 current_stage 的中文名与 progress', async () => {
    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('审查完成')
    expect(wrapper.find('.el-progress').exists()).toBe(true)
  })

  it('阶段认不出来时原样显示，不显示成"未知"', async () => {
    getContractsMock.mockResolvedValue([
      item({ latest_task: { task_id: 7, status: 'pending', current_stage: 'SOMETHING_NEW', progress: 10 } }),
    ])

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('SOMETHING_NEW')
  })

  it('latest_task 为 null 时显示"尚未发起审查"，且不显示进度条', async () => {
    getContractsMock.mockResolvedValue([item({ latest_task: null })])

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('尚未发起审查')
    expect(wrapper.find('.el-progress').exists()).toBe(false)
  })

  it('latest_task 为 null 时「查看审查」按钮禁用', async () => {
    getContractsMock.mockResolvedValue([item({ latest_task: null })])

    const { wrapper } = await mountView()

    const button = wrapper.findAll('button').find((b) => b.text().includes('查看审查'))
    expect(button).toBeDefined()
    expect(button!.attributes('disabled')).toBeDefined()
  })

  it('有任务时点击「查看审查」跳到该任务的 workbench', async () => {
    const { wrapper, router } = await mountView()
    const push = vi.spyOn(router, 'push')

    const button = wrapper.findAll('button').find((b) => b.text().includes('查看审查'))
    await button!.trigger('click')

    expect(push).toHaveBeenCalledWith('/review-tasks/7/workbench')
  })

  it('没有任务时点击不会跳转（不指向一个不存在的 task）', async () => {
    getContractsMock.mockResolvedValue([item({ latest_task: null })])
    const { wrapper, router } = await mountView()
    const push = vi.spyOn(router, 'push')

    const button = wrapper.findAll('button').find((b) => b.text().includes('查看审查'))
    await button!.trigger('click')

    expect(push).not.toHaveBeenCalled()
  })

  it('接口返回空数组时显示"暂无合同"', async () => {
    getContractsMock.mockResolvedValue([])

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('暂无合同')
    expect(wrapper.find('.el-table').exists()).toBe(false)
  })

  it('请求失败时显示错误，**不**伪装成"暂无合同"', async () => {
    getContractsMock.mockRejectedValue(new ApiError({ code: 'BACKEND_UNREACHABLE', message: '后端不可达' }))

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('加载合同列表失败')
    expect(wrapper.text()).toContain('后端不可达')
    // 关键区分：「没拿到数据」不能说成「没有数据」
    expect(wrapper.text()).not.toContain('暂无合同')
  })

  it('非 ApiError 的异常也给出可读提示', async () => {
    getContractsMock.mockRejectedValue(new Error('boom'))

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('加载合同列表失败')
  })

  it('刷新按钮会重新取数', async () => {
    const { wrapper } = await mountView()
    expect(getContractsMock).toHaveBeenCalledTimes(1)

    const refresh = wrapper.findAll('button').find((b) => b.text().includes('刷新'))
    await refresh!.trigger('click')
    await vi.waitFor(() => expect(getContractsMock).toHaveBeenCalledTimes(2))
  })
})
