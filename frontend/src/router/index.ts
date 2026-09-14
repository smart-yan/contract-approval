/**
 * 路由配置（架构文档 §2.2 router/index.ts）。
 *
 * 本阶段（P2-e）**只有壳路由**：
 *   /            → Layout（重定向到 /dashboard）
 *   /dashboard   → 审查大盘占位页
 *   /404         → 404 页面
 *   /:pathMatch  → 兜底到 404
 *
 * ⚠️ 刻意不创建 /contracts、/reviews、/rules、/tasks、/reports 等业务路由。
 * 角色守卫（架构文档里提到的 "路由 + 角色守卫"）属于 P4 认证阶段，本阶段不做。
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
    ],
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
