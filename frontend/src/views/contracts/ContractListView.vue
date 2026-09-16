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
 *
 * 上传（P14-5-1）：**受理即结束**。``POST /api/agent/review`` 回 202 + task_id 就
 * 表示"Agent 收下这份文件了"，审查在它自己的进程里跑 —— 因此这里**不等待结果**，
 * 拿到 task_id 就跳到工作台（``/review-tasks/{task_id}/workbench``），由工作台轮询
 * 真实状态。⚠️ 202 **不代表审查成功**：能不能审是后台图的事，跳过去看到的才是实情。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { genFileId } from 'element-plus'
import type { UploadInstance, UploadProps, UploadRawFile, UploadUserFile } from 'element-plus'

import {
  ACCEPTED_UPLOAD_EXTENSIONS,
  startContractReview,
  type ReviewAccepted,
} from '@/api/agent-review'
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

// --------------------------------------------------------------------------- //
// 上传并开始审查（P14-5-1）
// --------------------------------------------------------------------------- //
/**
 * 合同类型候选值。**取值必须与 Backend ``constants.ContractType`` 一致** ——
 * 它决定用哪一套规则集（§7.2），传一个后端不认的值会让规则审查拿不到快照。
 *
 * ⚠️ 这里只有 5 个选项、也只有本页用它，因此**不单独建 constants 文件**
 * （那会为了一个消费方造一层间接）。哪天第二个页面也要选类型，再提上去。
 */
const CONTRACT_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: 'PURCHASE', label: '采购' },
  { value: 'SALES', label: '销售' },
  { value: 'SERVICE', label: '服务' },
  { value: 'LABOR', label: '劳动' },
  { value: 'OTHER', label: '其它' },
]

const uploadVisible = ref(false)
const uploading = ref(false)
const uploadError = ref('')
const uploadRef = ref<UploadInstance>()
const fileList = ref<UploadUserFile[]>([])
const form = ref({ contractNo: '', title: '', contractType: 'PURCHASE' })

/** 打开对话框时**清空上一次的输入** —— 留着上次的合同号比空着更容易出错。 */
function openUpload(): void {
  uploadError.value = ''
  uploading.value = false
  fileList.value = []
  form.value = { contractNo: '', title: '', contractType: 'PURCHASE' }
  uploadRef.value?.clearFiles()
  uploadVisible.value = true
}

/**
 * ``limit=1`` 时再选一个文件会触发 exceed —— 按 Element Plus 文档的写法**换成**新文件，
 * 而不是把用户挡在外面（"只能选一个"不该表现为"点了没反应"）。
 */
const handleExceed: UploadProps['onExceed'] = (files) => {
  uploadRef.value?.clearFiles()
  const file = files[0] as UploadRawFile | undefined
  if (file) {
    file.uid = genFileId()
    uploadRef.value?.handleStart(file)
  }
}

/** 选中的原始 ``File``（还没选就是 null）。 */
const selectedFile = computed<File | null>(() => fileList.value[0]?.raw ?? null)

/** 提交前的**表单完整性检查**。后端与 Agent 各自还会再校验一次，这里只挡明显的手误。 */
function formProblem(): string {
  if (selectedFile.value === null) {
    return '请先选择合同文件'
  }
  if (form.value.contractNo.trim() === '') {
    return '请填写合同编号'
  }
  if (form.value.title.trim() === '') {
    return '请填写合同名称'
  }
  return ''
}

/**
 * 把 Agent 的拒绝翻译成一句人话。
 *
 * ⚠️ 与工作台的 ``reviewErrorMessage`` 同一手法：**422 且 ``code`` 回落到
 * ``NETWORK_ERROR``** 说明那是 FastAPI 的请求体校验（响应体是 ``{"detail": [...]}``，
 * 没有业务错误码），真实原因是"提交的参数不合法"，不是"网络错误"。
 * Agent 自己拒绝时（``workflow_status=rejected``）则带着业务错误码，
 * 直接展示它给的人话原因。
 */
function uploadErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return '发起审查失败，请稍后重试'
  }
  switch (error.code) {
    case 'BACKEND_UNREACHABLE':
      return 'Agent 连不上后端服务，请确认后端已启动'
    case 'BACKEND_REJECTED':
      return '后端拒绝了这次上传'
    case 'PARSE_UNSUPPORTED_TYPE':
      return '该文件类型暂不支持审查（当前只支持 DOCX）'
    case 'BACKEND_CONTRACT_INCOMPLETE':
      return '后端返回的数据不完整，无法开始审查'
    case 'TASK_NOT_FOUND':
      return '审查任务不存在'
    default:
      break
  }
  if (error.status === 422 && error.code === 'NETWORK_ERROR') {
    return '提交的内容不合法（请检查合同编号、名称与类型）'
  }
  return error.message
}

/**
 * 提交：拿 202 + task_id，**跳去工作台**，不等结果。
 *
 * ⚠️ 成功后刻意**不在这里刷新列表** —— 跳走之后本地这份列表已经没有意义，
 * 回来时 ``onMounted`` 会重新取。多刷一次只是两次请求里的一次浪费。
 */
async function submitUpload(): Promise<void> {
  const problem = formProblem()
  if (problem !== '') {
    uploadError.value = problem
    return
  }
  const file = selectedFile.value
  if (file === null) {
    return // formProblem 已经挡过，这里只是让类型收窄
  }

  uploading.value = true
  uploadError.value = ''
  try {
    const accepted: ReviewAccepted = await startContractReview({
      file,
      contractNo: form.value.contractNo.trim(),
      title: form.value.title.trim(),
      contractType: form.value.contractType,
    })
    uploadVisible.value = false
    void router.push(`/review-tasks/${accepted.task_id}/workbench`)
  } catch (error) {
    uploadError.value = uploadErrorMessage(error)
  } finally {
    uploading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-container">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>合同列表</span>
          <div>
            <el-button text :loading="loading" @click="load">刷新</el-button>
            <el-button type="primary" @click="openUpload">上传合同</el-button>
          </div>
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

    <!--
      上传合同（P14-5-1）。提交成功后**只拿到 202 + task_id**，随即跳到工作台 ——
      真正的"审查中 / 完成 / 被阻塞"都在那里显示，本对话框不假装知道结果。
    -->
    <el-dialog v-model="uploadVisible" title="上传合同并发起审查" width="520px">
      <el-alert
        v-if="uploadError"
        class="contracts__error"
        type="error"
        show-icon
        :closable="false"
        title="发起审查失败"
        :description="uploadError"
      />

      <el-form label-width="90px" @submit.prevent>
        <el-form-item label="合同文件" required>
          <el-upload
            ref="uploadRef"
            v-model:file-list="fileList"
            :auto-upload="false"
            :limit="1"
            :accept="ACCEPTED_UPLOAD_EXTENSIONS"
            :on-exceed="handleExceed"
          >
            <el-button>选择文件</el-button>
            <template #tip>
              <div class="el-upload__tip">当前只支持 DOCX（黄金链路）</div>
            </template>
          </el-upload>
        </el-form-item>

        <el-form-item label="合同编号" required>
          <el-input v-model="form.contractNo" maxlength="64" placeholder="如 HT-2026-001" />
        </el-form-item>

        <el-form-item label="合同名称" required>
          <el-input v-model="form.title" maxlength="255" placeholder="如 设备采购合同" />
        </el-form-item>

        <el-form-item label="合同类型" required>
          <el-select v-model="form.contractType" class="contracts__select">
            <el-option
              v-for="option in CONTRACT_TYPE_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submitUpload">上传并开始审查</el-button>
      </template>
    </el-dialog>
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

.contracts__select {
  width: 100%;
}
</style>
