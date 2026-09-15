import { mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App from '@/App.vue'
import { routes } from '@/router'
import { useAppStore } from '@/stores/app'

// 仪表盘挂载时会调用后端 /health。单元测试里不应真的发网络请求，
// 因此把整个 system 模块替换成固定响应 —— 顺带让"后端信息渲染"可断言。
vi.mock('@/api/system', () => ({
  fetchHealth: vi.fn().mockResolvedValue({
    status: 'ok',
    app_env: 'dev',
    version: '0.1.0',
    uptime_seconds: 1.23,
    checks: {
      database: {
        ok: true,
        database: 'contract_approval',
        server_version: '8.4.8',
        charset: 'utf8mb4',
        latency_ms: 1.5,
        error_code: null,
        error_type: null,
      },
      executors: {
        thread_pool_created: true,
        thread_pool_size: 16,
        ocr_executor_created: false,
        ocr_executor_type: null,
        ocr_executor_mode: 'process',
        ocr_max_workers: 1,
      },
    },
  }),
}))

/**
 * 挂载**真实的根组件 App.vue**，而不是直接挂 DefaultLayout。
 *
 * 这一点很关键：`/dashboard` 匹配到的路由链是 [DefaultLayout, DashboardView]。
 * 若直接 mount(DefaultLayout)，它内部的 `<router-view>` 处于深度 0，
 * 会再次解析出深度 0 的组件 —— 也就是 DefaultLayout 自己，导致布局被嵌套渲染两次。
 * 挂 App.vue 才能复现真实结构：App(深度0) → DefaultLayout(深度1) → DashboardView(深度2)。
 */
async function mountApp(pinia: Pinia): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({ history: createMemoryHistory(), routes })
  await router.push('/dashboard')
  await router.isReady()

  const wrapper = mount(App, {
    global: { plugins: [pinia, router, ElementPlus] },
  })
  // 等 DashboardView 的 onMounted → fetchHealth 的 promise 链结算
  await nextTick()
  await nextTick()

  return { wrapper, router }
}

describe('基础布局', () => {
  let pinia: Pinia

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
  })

  it('渲染侧边栏、顶栏与内容区', async () => {
    const { wrapper } = await mountApp(pinia)

    expect(wrapper.find('.sidebar').exists()).toBe(true)
    expect(wrapper.find('.header').exists()).toBe(true)
    expect(wrapper.find('.layout__aside').exists()).toBe(true)
    expect(wrapper.find('.layout__main').exists()).toBe(true)
  })

  it('布局只渲染一次（防止路由嵌套重复渲染）', async () => {
    const { wrapper } = await mountApp(pinia)

    expect(wrapper.findAll('.layout')).toHaveLength(1)
    expect(wrapper.findAll('.sidebar')).toHaveLength(1)
    expect(wrapper.findAll('.header')).toHaveLength(1)
  })

  it('侧边栏只列出已经落地的业务菜单项', async () => {
    // 【本用例的前身是 P2-e 的「唯一的真实菜单项」】
    // 那条断言在 P11-5 加入合同列表时必然失效 —— 按本意改写为
    // "**菜单项恰好是已批准的这几个**"，而不是删掉测试或放宽成"至少一个"。
    // 后者会让将来误加一个 disabled 占位菜单也悄悄通过。
    const { wrapper } = await mountApp(pinia)

    const labels = wrapper.findAll('.el-menu-item').map((item) => item.text())

    expect(labels).toEqual(['审查大盘', '合同列表'])
  })

  it('RouterView 渲染出 dashboard 占位页（Element Plus 生效）', async () => {
    const { wrapper } = await mountApp(pinia)

    // el-alert / el-card 能渲染，说明 Element Plus 已正确注册
    expect(wrapper.find('.el-alert').exists()).toBe(true)
    expect(wrapper.findAll('.el-card').length).toBeGreaterThan(0)
    expect(wrapper.text()).toContain('壳工程占位页')
  })

  it('后端连通性由 mock 数据渲染（Axios 层被正确调用）', async () => {
    const { wrapper } = await mountApp(pinia)

    expect(wrapper.text()).toContain('正常')
    expect(wrapper.text()).toContain('contract_approval')
    expect(wrapper.text()).toContain('8.4.8')
  })

  it('折叠状态由 Pinia store 驱动', async () => {
    const { wrapper } = await mountApp(pinia)
    const store = useAppStore()

    expect(wrapper.find('.layout__aside').attributes('style')).toContain('220px')

    store.toggleSidebar()
    await nextTick()

    expect(wrapper.find('.layout__aside').attributes('style')).toContain('64px')
  })

  it('顶栏按钮可以切换 store 中的折叠状态', async () => {
    const { wrapper } = await mountApp(pinia)
    const store = useAppStore()

    expect(store.sidebarCollapsed).toBe(false)
    await wrapper.find('.header button').trigger('click')
    expect(store.sidebarCollapsed).toBe(true)
  })
})

describe('404 页面', () => {
  it('未匹配路由渲染 404 页面并显示请求路径', async () => {
    const piniaInstance = createPinia()
    setActivePinia(piniaInstance)

    const router = createRouter({ history: createMemoryHistory(), routes })
    await router.push('/definitely/not/here')
    await router.isReady()

    const wrapper = mount(App, {
      global: { plugins: [piniaInstance, router, ElementPlus] },
    })

    expect(wrapper.text()).toContain('404')
    expect(wrapper.text()).toContain('/definitely/not/here')
  })
})
