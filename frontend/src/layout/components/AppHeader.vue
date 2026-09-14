<script setup lang="ts">
/**
 * 顶栏：侧边栏折叠开关 + 版本标记。
 *
 * 刻意不放置用户头像 / 退出登录等入口 —— 认证体系属于 P4 阶段。
 */
import { Expand, Fold } from '@element-plus/icons-vue'
import { computed } from 'vue'

import { APP_VERSION } from '@/constants/app'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()

const collapsed = computed(() => appStore.sidebarCollapsed)
// 展开状态下点击 = 折叠，所以显示「折叠」图标，语义与动作一致
const toggleIcon = computed(() => (collapsed.value ? Expand : Fold))
</script>

<template>
  <div class="header">
    <el-button
      text
      :icon="toggleIcon"
      aria-label="切换侧边栏"
      @click="appStore.toggleSidebar()"
    />

    <div class="header__spacer" />

    <el-tag type="info" size="small" disable-transitions>v{{ APP_VERSION }}</el-tag>
  </div>
</template>

<style scoped>
.header {
  display: flex;
  align-items: center;
  width: 100%;
}

.header__spacer {
  flex: 1 1 auto;
}
</style>
