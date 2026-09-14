<script setup lang="ts">
/**
 * 侧边栏导航。
 *
 * 本阶段只有「审查大盘」一个真实菜单项。
 * 刻意**不添加**合同/任务/风险/规则/报告等 disabled 占位菜单 ——
 * 那会提前制造出并不存在的业务结构；等对应阶段落地时再逐项加入。
 */
import { DataAnalysis } from '@element-plus/icons-vue'
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { APP_NAME } from '@/constants/app'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()

const collapsed = computed(() => appStore.sidebarCollapsed)

/** el-menu 用 index 与当前路由匹配高亮 */
const activeMenu = computed(() => route.path)
</script>

<template>
  <div class="sidebar">
    <div class="sidebar__brand" :title="APP_NAME">
      <span v-if="collapsed">CA</span>
      <span v-else>{{ APP_NAME }}</span>
    </div>

    <el-menu
      class="sidebar__menu"
      :default-active="activeMenu"
      :collapse="collapsed"
      :collapse-transition="false"
      router
    >
      <el-menu-item index="/dashboard">
        <el-icon><DataAnalysis /></el-icon>
        <template #title>审查大盘</template>
      </el-menu-item>
    </el-menu>
  </div>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  background-color: var(--el-bg-color);
  border-right: 1px solid var(--el-border-color-light);
}

.sidebar__brand {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 56px;
  flex: 0 0 56px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  border-bottom: 1px solid var(--el-border-color-light);
}

.sidebar__menu {
  flex: 1 1 auto;
  border-right: none;
}
</style>
