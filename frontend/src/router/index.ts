/**
 * 路由配置（架构文档 §2.2 router/index.ts）。
 *
 * 路由演进：
 *   P2-e  /                  → Layout（重定向到 /dashboard）
 *         /dashboard         → 审查大盘壳工程占位页
 *   P11-5 /contracts         → 合同列表（真实业务页面，取 Backend 的 /api/v1/contracts）
 *         /review-tasks/:taskId/workbench → 审查工作台（P11-6 骨架 + P11-7 风险定位）
 *   其它  /404、/:pathMatch  → 兜底 404
 *
 * ⚠️ 仍然刻意没有 /reviews、/rules、/tasks、/reports —— 它们对应的页面还不存在。
 * 新增业务路由时请同步更新 ``tests/unit/router.spec.ts`` 的白名单守卫
 * （它把这批路径钉死，防止有人未经裁决就往里加页面）。
 *
 * 角色守卫（架构文档里提到的 "路由 + 角色守卫"）属于 P4 认证阶段，仍未做。
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { APP_NAME } from '@/constants/app'
import DefaultLayout from '@/layout/DefaultLayout.vue'

export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: DefaultLayout,
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/dashboard/DashboardView.vue'),
        meta: { title: '审查大盘' },
      },
      {
        // P11-5：合同列表。数据来自 Backend 的 GET /api/v1/contracts（走 vite 的 /api 代理）
        path: 'contracts',
        name: 'contracts',
        component: () => import('@/views/contracts/ContractListView.vue'),
        meta: { title: '合同列表' },
      },
    ],
  },
  {
    // P11-5：工作台路由。合同列表的「查看审查」跳到这里。
    // 页面自身在 P11-6（骨架：任务/合同/文件/条款/元数据/风险/原文）与
    // P11-7（风险卡片 → paragraph_index → 滚动高亮）落地。
    path: '/review-tasks/:taskId/workbench',
    name: 'workbench',
    component: () => import('@/views/workbench/WorkbenchView.vue'),
    meta: { title: '审查工作台' },
  },
  {
    path: '/404',
    name: 'not-found',
    component: () => import('@/views/error/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
  {
    // 兜底路由：未匹配的路径直接渲染 404 页面。
    // 刻意用「渲染」而不是「重定向到 /404」—— 保持用户输入的 URL 不变，便于排查问题。
    path: '/:pathMatch(.*)*',
    name: 'catch-all',
    component: () => import('@/views/error/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

// 用路由 meta 驱动浏览器标题。放在 afterEach 而不是 beforeEach，避免影响导航时序。
router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} - ${APP_NAME}` : APP_NAME
})

export default router
