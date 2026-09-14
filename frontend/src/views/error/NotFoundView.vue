<script setup lang="ts">
/**
 * 404 页面。
 *
 * 同时服务于两个路由：显式的 `/404` 与兜底路由 `/:pathMatch(.*)*`。
 * 因为兜底路由保留原始 URL（不重定向），所以这里把用户输入的路径显示出来，便于排查。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const attemptedPath = computed(() => route.fullPath)
</script>

<template>
  <div class="not-found">
    <el-result icon="warning" title="404" sub-title="抱歉，你访问的页面不存在">
      <template #extra>
        <p class="not-found__path">请求路径：{{ attemptedPath }}</p>
        <el-button type="primary" @click="router.push('/dashboard')">返回审查大盘</el-button>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.not-found {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 60vh;
}

.not-found__path {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-family: Consolas, Monaco, monospace;
  word-break: break-all;
}
</style>
