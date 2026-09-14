/**
 * 应用外壳状态（shell state）。
 *
 * ⚠️ 这里**只放界面外壳的状态**（侧边栏折叠等）。
 * 业务状态（user / task / workspace，架构文档 §2.2）属于后续业务阶段，
 * 本阶段刻意不创建 —— 避免提前制造业务结构。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useAppStore = defineStore('app', () => {
  /** 侧边栏是否折叠 */
  const sidebarCollapsed = ref(false)

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  function setSidebarCollapsed(collapsed: boolean): void {
    sidebarCollapsed.value = collapsed
  }

  return { sidebarCollapsed, toggleSidebar, setSidebarCollapsed }
})
