<script setup lang="ts">
/**
 * 审查大盘 —— **P2-e 壳工程占位页**。
 *
 * 本页现在只做两件事：
 *   1. 验证壳工程链路可用（Router / Layout / Element Plus / Pinia / Axios 都能工作）；
 *   2. 显示后端连通状态，作为前后端之间的第一个真实连接点。
 *
 * ⚠️ 下方所有统计数字都是**静态占位（mock）**，不是真实数据。
 * 真实的审查统计来自后续阶段的业务接口，此处刻意不建立任何业务模型或 API 调用。
 */
import { onMounted, ref } from 'vue'

import { ApiError } from '@/api/request'
import { fetchHealth, type HealthResponse } from '@/api/system'
import { APP_NAME, APP_VERSION } from '@/constants/app'

type ConnectionState = 'loading' | 'ok' | 'degraded' | 'unreachable'

const connectionState = ref<ConnectionState>('loading')
const health = ref<HealthResponse | null>(null)
const errorMessage = ref('')

async function checkBackend(): Promise<void> {
  connectionState.value = 'loading'
  health.value = null
  errorMessage.value = ''

  try {
    const data = await fetchHealth()
    health.value = data
    connectionState.value = data.status === 'ok' ? 'ok' : 'degraded'
  } catch (error) {
    connectionState.value = 'unreachable'
    errorMessage.value =
      error instanceof ApiError ? error.message : '无法连接后端服务，请确认后端已启动'
  }
}

onMounted(checkBackend)

/**
 * ⚠️ 静态占位数据 —— 仅用于验证布局与组件渲染，**不代表任何真实业务数据**。
 * 后续阶段接入审查任务接口后，这里会替换为真实统计。
 */
const PLACEHOLDER_STATS = [
  { label: '待审合同', value: '--' },
  { label: '审查中', value: '--' },
  { label: '已完成', value: '--' },
  { label: '阻塞任务', value: '--' },
] as const
</script>

<template>
  <div class="page-container">
    <el-alert
      class="dashboard__notice"
      type="info"
      show-icon
      :closable="false"
      title="壳工程占位页"
      description="本页为 P2-e 阶段的前端基础壳，用于验证路由、布局与组件链路。下方统计数字为静态占位数据，真实业务接口将在后续阶段接入。"
    />

    <el-row :gutter="16">
      <el-col :xs="24" :md="14">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>后端连通性</span>
              <el-button text :loading="connectionState === 'loading'" @click="checkBackend">
                重新检测
              </el-button>
            </div>
          </template>

          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="状态">
              <el-tag v-if="connectionState === 'loading'" type="info">检测中…</el-tag>
              <el-tag v-else-if="connectionState === 'ok'" type="success">正常</el-tag>
              <el-tag v-else-if="connectionState === 'degraded'" type="warning">
                依赖异常（数据库不可用）
              </el-tag>
              <el-tag v-else type="danger">无法连接</el-tag>
            </el-descriptions-item>

            <el-descriptions-item v-if="health" label="运行环境">
              {{ health.app_env }} / v{{ health.version }}
            </el-descriptions-item>

            <el-descriptions-item v-if="health" label="数据库">
              <template v-if="health.checks.database.ok">
                {{ health.checks.database.database }}
                （MySQL {{ health.checks.database.server_version }} ·
                {{ health.checks.database.charset }} ·
                {{ health.checks.database.latency_ms }} ms）
              </template>
              <template v-else>
                不可用（{{ health.checks.database.error_code }}）
              </template>
            </el-descriptions-item>

            <el-descriptions-item v-if="health" label="执行器">
              线程池 {{ health.checks.executors.thread_pool_size }} ·
              OCR {{ health.checks.executors.ocr_executor_mode }} /
              {{ health.checks.executors.ocr_max_workers }}
              （{{ health.checks.executors.ocr_executor_created ? '已创建' : '未创建（懒加载）' }}）
            </el-descriptions-item>

            <el-descriptions-item v-if="errorMessage" label="错误信息">
              <span class="dashboard__error">{{ errorMessage }}</span>
            </el-descriptions-item>
          </el-descriptions>

          <p class="dashboard__hint">
            提示：请先在 <code>backend/</code> 下启动
            <code>uv run uvicorn app.main:app --port 8000</code>，再点击「重新检测」。
          </p>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="10">
        <el-card shadow="never">
          <template #header><span>系统信息</span></template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="系统名称">{{ APP_NAME }}</el-descriptions-item>
            <el-descriptions-item label="前端版本">v{{ APP_VERSION }}</el-descriptions-item>
            <el-descriptions-item label="当前阶段">P2-e 前端基础壳工程</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>

    <el-row class="dashboard__stats" :gutter="16">
      <el-col v-for="item in PLACEHOLDER_STATS" :key="item.label" :xs="12" :md="6">
        <el-card shadow="never">
          <div class="stat">
            <div class="stat__value">{{ item.value }}</div>
            <div class="stat__label">{{ item.label }}（占位）</div>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.dashboard__notice {
  margin-bottom: 16px;
}

.dashboard__stats {
  margin-top: 16px;
}

.dashboard__error {
  color: var(--el-color-danger);
}

.dashboard__hint {
  margin: 12px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.stat {
  text-align: center;
}

.stat__value {
  font-size: 24px;
  font-weight: 600;
  line-height: 1.4;
}

.stat__label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
