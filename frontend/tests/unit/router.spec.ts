import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { describe, expect, it } from 'vitest'

import { routes } from '@/router'

function createTestRouter(): Router {
  return createRouter({ history: createMemoryHistory(), routes })
}

describe('路由配置', () => {
  it('根路径重定向到 /dashboard', async () => {
    const router = createTestRouter()
    await router.push('/')
    await router.isReady()
    expect(router.currentRoute.value.path).toBe('/dashboard')
  })

  it('/dashboard 解析到 dashboard 命名路由', async () => {
    const router = createTestRouter()
    await router.push('/dashboard')
    expect(router.currentRoute.value.name).toBe('dashboard')
  })

  it('/404 解析到 not-found 命名路由', async () => {
    const router = createTestRouter()
    await router.push('/404')
    expect(router.currentRoute.value.name).toBe('not-found')
  })

  it('未匹配路径由兜底路由渲染 404，并保留原始 URL', async () => {
    const router = createTestRouter()
    await router.push('/some/unknown/path')
    expect(router.currentRoute.value.name).toBe('catch-all')
    // 刻意不重定向，URL 保持原样便于排查
    expect(router.currentRoute.value.fullPath).toBe('/some/unknown/path')
  })

  it('每个路由都声明了标题，供 afterEach 设置 document.title', () => {
    for (const route of routes) {
      if (route.children) {
        for (const child of route.children) {
          expect(child.meta?.title, `子路由 ${child.path} 缺少 meta.title`).toBeTruthy()
        }
      } else {
        expect(route.meta?.title, `路由 ${route.path} 缺少 meta.title`).toBeTruthy()
      }
    }
  })

  it('/contracts 解析到 contracts 命名路由', async () => {
    const router = createTestRouter()
    await router.push('/contracts')
    expect(router.currentRoute.value.name).toBe('contracts')
  })

  it('工作台路由带上 taskId 参数', async () => {
    const router = createTestRouter()
    await router.push('/review-tasks/42/workbench')
    expect(router.currentRoute.value.name).toBe('workbench')
    expect(router.currentRoute.value.params.taskId).toBe('42')
  })

  it('业务路由白名单守卫', () => {
    const allPaths = routes.flatMap((route) => [
      route.path,
      ...(route.children ?? []).map((child) => `${route.path}${child.path}`),
    ])

    // 【本用例的前身是 P2-e 的「不存在任何业务路由」】
    // 那条断言在 P11-5 加入合同列表时必然失效 —— 按守卫的本意改写为
    // "**只允许已批准的业务路由出现**"，而不是删掉测试。
    //
    // 白名单演进：
    //   P2-e   只有壳路由（/dashboard、/404）
    //   P11-5  /contracts、/review-tasks/:taskId/workbench
    const approvedBusinessRoutes = [
      '/contracts',
      '/review-tasks/:taskId/workbench',
    ]

    for (const path of approvedBusinessRoutes) {
      expect(allPaths, `已批准的业务路由 ${path} 丢了`).toContain(path)
    }

    // 这些属于后续阶段。若有人提前加了它们，这条用例会失败。
    const forbidden = ['/reviews', '/rules', '/tasks', '/reports', '/login']
    for (const path of forbidden) {
      expect(allPaths, `不应提前加入业务路由 ${path}`).not.toContain(path)
    }
  })
})
