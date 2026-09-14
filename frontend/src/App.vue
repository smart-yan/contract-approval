<script setup lang="ts">
// 根组件只做一件事：把路由内容渲染出来。
// 布局由 router/index.ts 里的 Layout 路由承担，而不是写死在这里 ——
// 这样将来 404、登录页等「不需要侧边栏」的页面可以独立于布局存在。
</script>

<template>
  <router-view />
</template>
