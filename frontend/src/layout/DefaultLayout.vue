<script setup lang="ts">
/**
 * 基础布局：Sidebar + Header + Main Content + RouterView。
 *
 * 只负责结构与尺寸，不承载任何业务逻辑。
 * 侧边栏宽度由 app store 的折叠状态驱动，Header 里的按钮切换该状态。
 */
import { computed } from 'vue'

import { SIDEBAR_COLLAPSED_WIDTH, SIDEBAR_WIDTH } from '@/constants/app'
import { useAppStore } from '@/stores/app'

import AppHeader from './components/AppHeader.vue'
import AppSidebar from './components/AppSidebar.vue'

const appStore = useAppStore()

const asideWidth = computed(() =>
  appStore.sidebarCollapsed ? `${SIDEBAR_COLLAPSED_WIDTH}px` : `${SIDEBAR_WIDTH}px`,
)
</script>

<template>
  <el-container class="layout">
    <el-aside class="layout__aside" :width="asideWidth">
      <AppSidebar />
    </el-aside>

    <el-container class="layout__body">
      <el-header class="layout__header" height="56px">
        <AppHeader />
      </el-header>

      <el-main class="layout__main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100%;
}

.layout__aside {
  /* 折叠动画与 el-menu 的 collapse-transition 保持一致的观感 */
  transition: width 0.2s;
  overflow-x: hidden;
}

.layout__body {
  /* 让 main 区域独立滚动，而不是整页滚动 */
  min-width: 0;
  height: 100%;
}

.layout__header {
  display: flex;
  align-items: center;
  padding: 0 16px;
  background-color: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-light);
}

.layout__main {
  padding: 0;
  overflow: auto;
}
</style>
