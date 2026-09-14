import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useAppStore } from '@/stores/app'

describe('app store（外壳状态）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('初始状态：侧边栏展开', () => {
    const store = useAppStore()
    expect(store.sidebarCollapsed).toBe(false)
  })

  it('toggleSidebar 在两个状态之间切换', () => {
    const store = useAppStore()

    store.toggleSidebar()
    expect(store.sidebarCollapsed).toBe(true)

    store.toggleSidebar()
    expect(store.sidebarCollapsed).toBe(false)
  })

  it('setSidebarCollapsed 可显式设置', () => {
    const store = useAppStore()

    store.setSidebarCollapsed(true)
    expect(store.sidebarCollapsed).toBe(true)

    // 幂等：重复设置同一个值不应翻转
    store.setSidebarCollapsed(true)
    expect(store.sidebarCollapsed).toBe(true)
  })

  it('每个 Pinia 实例持有独立状态', () => {
    const first = useAppStore()
    first.setSidebarCollapsed(true)

    setActivePinia(createPinia())
    const second = useAppStore()
    expect(second.sidebarCollapsed).toBe(false)
  })
})
