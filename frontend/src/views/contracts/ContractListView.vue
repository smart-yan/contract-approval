<script setup lang="ts">
/**
 * 合同列表页（P11-5）。
 *
 * 数据来源只有一个：``GET /api/v1/contracts``（Backend P11-3）。
 *
 * ⚠️ **状态只认 ``latest_task``**：
 * 列表的"审查状态 / 进度"全部来自 ``latest_task.current_stage`` 与 ``latest_task.progress``。
 * 前端**不知道** ``contract.status`` / ``contract.current_task_id`` 这两个字段的存在 ——
 * 它们在 Backend 侧是过期冗余字段（P4 之后再没人维护），接口也刻意不返回，
 * 因此这里不可能误用。``latest_task === null`` 就如实显示"尚未发起审查"，不伪造任务。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { getContracts, type ContractListItem } from '@/api/contracts'
import { ApiError } from '@/api/request'
import { taskStageLabel } from '@/constants/review'
import { formatUtcTimestamp } from '@/utils/datetime'

const router = useRouter()

const contracts = ref<ContractListItem[]>([])
const loading = ref(false)
const errorMessage = ref('')

/** 已经有任务 → 可以去工作台；没有 → 按钮禁用（不跳一个不存在的 task）。 */
function reviewable(row: ContractListItem): boolean {
  return row.latest_task !== null
}

const isEmpty = computed(() => !loading.value && !errorMessage.value && contracts.value.length === 0)

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''

  try {
    contracts.value = await getContracts()
  } catch (error) {
    // ⚠️ **不静默失败**：接口挂了就必须让用户看见，否则页面会显示成"暂无合同"，
    // 把"没拿到数据"说成"没有数据"——这两件事完全不同。
    contracts.value = []
    errorMessage.value = error instanceof ApiError ? error.message : '加载合同列表失败'
  } finally {
    loading.value = false
  }
}

function openWorkbench(row: ContractListItem): void {
  if (row.latest_task === null) {
    return
  }
  void router.push(`/review-tasks/${row.latest_task.task_id}/workbench`)
}

onMounted(load)
</script>

<template>
  <div class="page-container">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>合同列表</span>
          <el-button text :loading="loading" @click="load">刷新</el-button>
        </div>
      </template>

      <el-alert
        v-if="errorMessage"
        class="contracts__error"
        type="error"
        show-icon
        :closable="false"
        title="加载合同列表失败"
        :description="errorMessage"
      />

      <el-empty v-if="isEmpty" description="暂无合同" />

      <el-table v-else v-loading="loading" :data="contracts" row-key="contract_id" stripe>
        <el-table-column prop="contract_no" label="合同编号" min-width="180" />
        <el-table-column prop="title" label="合同名称" min-width="200" show-overflow-tooltip />
        <el-table-column prop="contract_type" label="合同类型" width="120" />

        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatUtcTimestamp(row.created_at) }}</template>
        </el-table-column>

        <el-table-column label="审查状态" width="150">
          <template #default="{ row }">
            <span v-if="row.latest_task === null" class="contracts__muted">尚未发起审查</span>
            <el-tag v-else type="info">{{ taskStageLabel(row.latest_task.current_stage) }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="进度" width="160">
          <template #default="{ row }">
            <el-progress
              v-if="row.latest_task !== null"
              :percentage="row.latest_task.progress"
              :stroke-width="12"
            />
            <span v-else class="contracts__muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button
              type="primary"
              link
              :disabled="!reviewable(row)"
              :title="reviewable(row) ? '' : '尚未发起审查'"
              @click="openWorkbench(row)"
            >
              查看审查
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.contracts__error {
  margin-bottom: 12px;
}

.contracts__muted {
  color: var(--el-text-color-secondary);
}
</style>
