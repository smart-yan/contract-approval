<script setup lang="ts">
/**
 * 审查工作台（P11-6 骨架 + P11-7 风险定位）。
 *
 * 一次请求拿齐渲染所需的全部数据：``GET /api/v1/review-tasks/{taskId}/workbench``。
 * 页面把它分成几块展示：任务 → 合同 → 文件 → 元数据 → 条款 → 风险 → 原文。
 *
 * 风险定位（P11-7）：点风险卡片 → 读 ``risk.paragraph_index`` → 找原文里同号的
 * ``[data-paragraph-index]`` → 滚动 + 高亮。**前端只做这一跳**：``paragraph_index``
 * 是 P10 冻结的定位坐标，语义定位（quote → 段落）是 Agent 的职责（P9），
 * 在这里重做一遍等于把同一条契约实现两次。
 *
 * ⚠️ 数据**只读**：页面不修改 API 返回的任何对象（不在 risk / block 上挂
 * ``highlighted`` / ``active`` 之类的 UI 状态）。选中状态一律另开 ref 维护。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/request'
import { getReviewTaskWorkbench, type WorkbenchResponse, type WorkbenchRisk } from '@/api/workbench'
import { taskStageLabel } from '@/constants/review'
import { formatUtcTimestamp } from '@/utils/datetime'

const route = useRoute()
const router = useRouter()

const workbench = ref<WorkbenchResponse | null>(null)
const loading = ref(false)
const errorMessage = ref('')
/** 任务不存在与"加载失败"是两件事，界面与文案都不同，因此分开记。 */
const notFound = ref(false)

/**
 * 当前被定位到的段落序号（P11-7）。``null`` = 没有选中任何段落。
 *
 * ⚠️ 这是**页面自己的 UI 状态**，与 API 返回的 blocks / risks 完全无关 ——
 * 高亮靠它比对出来，而不是往 block 对象上写 ``highlighted = true``。
 * 那份响应是一次快照，被就地改写后就再也说不清"原始数据是什么样"。
 */
const activeParagraphIndex = ref<number | null>(null)

/**
 * 定位失败时的轻量提示（空串 = 不显示）。
 *
 * ⚠️ 刻意**不用 ElMessage**：项目里目前没有任何全局通知的用法，为一个提示引入
 * 一套挂在 body 上的消息系统，既有额外测试成本，也让"提示"这件事出现两种写法。
 * 一个页面内的 ref 就够，而且它就显示在风险列表上方 —— 提示与触发它的操作同屏。
 */
const locateHint = ref('')

/** 原文容器：定位用的 DOM 查询**限定在这一块之内**，不扫全文档。 */
const blocksRef = ref<HTMLElement | null>(null)

/**
 * 从路由取 taskId 并做**最小**校验。
 *
 * 路由是 ``/review-tasks/:taskId/workbench``，正常情况下参数一定在；
 * 但如果有人手敲了一个 ``/review-tasks/abc/workbench``，不该把它拼进 URL
 * 去发一个注定 404 的请求 —— 直接当作"任务不存在"处理。
 */
const taskId = computed<number | null>(() => {
  const raw = route.params.taskId
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== 'string' || !/^\d+$/.test(value)) {
    return null
  }
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
})

/** 风险等级 → Element Plus 的 tag 类型。**只有三档**，不发明第四种。 */
const RISK_LEVEL_TAG: Record<string, 'danger' | 'warning' | 'info'> = {
  HIGH: 'danger',
  MEDIUM: 'warning',
  LOW: 'info',
}

/** 认不出来的等级按 info 显示、文字原样 —— 不隐藏、也不假装它是 LOW。 */
function riskLevelTag(level: string): 'danger' | 'warning' | 'info' {
  return RISK_LEVEL_TAG[level] ?? 'info'
}

/** 只取摘要展示，但**不改动原值**（完整值放在 title 里，鼠标悬停可见）。 */
function shortHash(sha256: string): string {
  return sha256.length > 16 ? `${sha256.slice(0, 8)}…${sha256.slice(-8)}` : sha256
}

/** 段落区间；换算不出来时返回空串（由模板显示"位置未知"）。 */
function paragraphRange(start: number | null, end: number | null): string {
  if (start === null || end === null) {
    return ''
  }
  return start === end ? `第 ${start} 段` : `第 ${start}–${end} 段`
}

/**
 * 点风险卡片 → 定位到原文里 ``risk.paragraph_index`` 那一段（P11-7）。
 *
 * ⚠️ 定位依据**只有** ``paragraph_index``。这里**不做任何文本搜索** ——
 * 不拿 ``original_text`` / ``reason`` 去原文里找，不做模糊匹配或正则，
 * 也不从 clause 反推段落号。P9 已经把"语义 → 坐标"这一步在 Agent 侧做完了，
 * 前端再来一次就是同一份契约的两套实现，两边漂移时不会有任何报错。
 *
 * 三种走不通的情况（null / 非整数 / DOM 里没有这一段）一律**不抛异常**：
 * 没有可去的地方不是错误，但要如实告诉用户，不能装作点了。
 */
function handleRiskLocate(risk: WorkbenchRisk): void {
  locateHint.value = ''

  const index = risk.paragraph_index

  // 没有坐标：不发定位、不报错。⚠️ 同时**清掉旧高亮** —— 用户刚点了另一条风险，
  // 留着上一段亮着会让人以为它跟这条风险有关，那是错的定位而不是"没定位"。
  if (index === null || !Number.isInteger(index)) {
    activeParagraphIndex.value = null
    locateHint.value = '该风险没有标注原文段落，无法定位'
    return
  }

  // ⚠️ index 已过 Number.isInteger（纯整数），拼进选择器的不可能是任意字符串
  const target = blocksRef.value?.querySelector(`[data-paragraph-index="${index}"]`) ?? null
  if (!(target instanceof HTMLElement)) {
    activeParagraphIndex.value = null
    locateHint.value = `未找到对应的原文段落（第 ${index} 段）`
    return
  }

  activeParagraphIndex.value = index
  // 用元素自身滚动，不碰 window.scrollTo / scrollTop —— 滚动容器交给浏览器判断
  target.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  notFound.value = false

  const id = taskId.value
  if (id === null) {
    // 参数不合法 —— 不发请求，也不伪装成"加载失败"
    notFound.value = true
    loading.value = false
    return
  }

  try {
    workbench.value = await getReviewTaskWorkbench(id)
  } catch (error) {
    workbench.value = null
    if (error instanceof ApiError && error.code === 'TASK_NOT_FOUND') {
      notFound.value = true
    } else {
      // ⚠️ 不静默失败，也**不显示成"暂无数据"** —— 「没拿到数据」与「没有数据」
      // 是两件事，混淆会让人以为合同真的没内容。
      errorMessage.value = error instanceof ApiError ? error.message : '加载审查工作台失败'
    }
  } finally {
    loading.value = false
  }
}

function backToContracts(): void {
  // 显式导航，不依赖浏览器 history —— 从别处直接打开本页时 history 里可能没有上一页
  void router.push('/contracts')
}

onMounted(load)
</script>

<template>
  <div class="page-container">
    <div class="workbench__toolbar">
      <el-button text @click="backToContracts">← 返回合同列表</el-button>
      <span v-if="workbench" class="workbench__heading">
        {{ workbench.contract.title }}
        <span class="workbench__muted">（{{ workbench.contract.contract_no }}）</span>
      </span>
      <el-button text :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card v-if="notFound" shadow="never">
      <el-result icon="warning" title="审查任务不存在" :sub-title="`任务 ${route.params.taskId} 不存在或已被删除`">
        <template #extra>
          <el-button type="primary" @click="backToContracts">返回合同列表</el-button>
        </template>
      </el-result>
    </el-card>

    <el-card v-else-if="errorMessage" shadow="never">
      <el-alert
        type="error"
        show-icon
        :closable="false"
        title="加载审查工作台失败"
        :description="errorMessage"
      />
      <div class="workbench__actions">
        <el-button @click="backToContracts">返回合同列表</el-button>
        <el-button type="primary" @click="load">重试</el-button>
      </div>
    </el-card>

    <template v-else>
      <el-skeleton v-if="loading && !workbench" :rows="6" animated />

      <template v-if="workbench">
        <!-- ---------------- 任务 ---------------- -->
        <el-card class="workbench__section" shadow="never">
          <template #header><span>审查任务</span></template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="审查阶段">
              <el-tag>{{ taskStageLabel(workbench.task.current_stage) }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="进度">
              <el-progress :percentage="workbench.task.progress" :stroke-width="12" />
            </el-descriptions-item>
            <el-descriptions-item label="任务状态">{{ workbench.task.status }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">
              {{ formatUtcTimestamp(workbench.task.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="结束时间">
              {{ workbench.task.finished_at ? formatUtcTimestamp(workbench.task.finished_at) : '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="综合等级">
              {{ workbench.task.risk_level_final ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="审查结论" :span="3">
              {{ workbench.task.conclusion ?? '—' }}
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <!-- ---------------- 合同 ---------------- -->
        <el-card class="workbench__section" shadow="never">
          <template #header><span>合同信息</span></template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="合同编号">{{ workbench.contract.contract_no }}</el-descriptions-item>
            <el-descriptions-item label="合同名称">{{ workbench.contract.title }}</el-descriptions-item>
            <el-descriptions-item label="合同类型">{{ workbench.contract.contract_type }}</el-descriptions-item>
            <el-descriptions-item label="我方主体">{{ workbench.contract.our_party ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="相对方">{{ workbench.contract.counterparty ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="金额">
              <template v-if="workbench.contract.amount">
                {{ workbench.contract.amount }} {{ workbench.contract.currency ?? '' }}
              </template>
              <template v-else>—</template>
            </el-descriptions-item>
            <el-descriptions-item label="签署日">{{ workbench.contract.sign_date ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="生效日">{{ workbench.contract.effective_date ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="到期日">{{ workbench.contract.expire_date ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="送审部门">{{ workbench.contract.dept ?? '—' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>

        <!-- ---------------- 文件 ---------------- -->
        <el-card class="workbench__section" shadow="never">
          <template #header><span>文件信息</span></template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="文件名">{{ workbench.file.file_name }}</el-descriptions-item>
            <el-descriptions-item label="文件类型">{{ workbench.file.file_type ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="解析状态">{{ workbench.file.parse_status }}</el-descriptions-item>
            <el-descriptions-item label="SHA-256" :span="3">
              <span class="workbench__hash" :title="workbench.file.sha256">
                {{ shortHash(workbench.file.sha256) }}
              </span>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <!-- ---------------- 元数据 ---------------- -->
        <el-card class="workbench__section" shadow="never">
          <template #header><span>合同信息提取（{{ workbench.metadata.length }}）</span></template>
          <el-empty v-if="workbench.metadata.length === 0" description="暂无提取到的合同信息" />
          <el-descriptions v-else :column="2" border size="small">
            <el-descriptions-item
              v-for="item in workbench.metadata"
              :key="item.field_key"
              :label="item.field_label"
            >
              {{ item.field_value }}
              <span class="workbench__muted">
                （{{ item.value_type }} · {{ item.extract_method }}<template
                  v-if="item.source_paragraph_index !== null"
                > · 第 {{ item.source_paragraph_index }} 段</template>）
              </span>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <!-- ---------------- 条款 ---------------- -->
        <el-card class="workbench__section" shadow="never">
          <template #header><span>条款（{{ workbench.clauses.length }}）</span></template>
          <el-empty v-if="workbench.clauses.length === 0" description="暂无条款" />
          <div v-else class="clauses">
            <div v-for="clause in workbench.clauses" :key="clause.clause_id" class="clause">
              <div class="clause__header">
                <strong>{{ clause.clause_no ?? '（无编号）' }}</strong>
                <span v-if="clause.title" class="clause__title">{{ clause.title }}</span>
                <el-tag size="small" type="info">{{ clause.clause_type }}</el-tag>
                <span class="workbench__muted">
                  <template v-if="paragraphRange(clause.start_paragraph_index, clause.end_paragraph_index)">
                    {{ paragraphRange(clause.start_paragraph_index, clause.end_paragraph_index) }}
                  </template>
                  <template v-else>位置未知</template>
                </span>
              </div>
              <div class="clause__text">{{ clause.text }}</div>
            </div>
          </div>
        </el-card>

        <!-- ---------------- 风险 ---------------- -->
        <el-card class="workbench__section" shadow="never">
          <template #header><span>风险（{{ workbench.risks.length }}）</span></template>
          <el-empty v-if="workbench.risks.length === 0" description="暂无风险" />
          <div v-else class="risks">
            <!-- 定位失败时的轻量提示（页内状态，不引入全局通知） -->
            <el-alert
              v-if="locateHint"
              class="risks__hint"
              type="warning"
              :closable="false"
              show-icon
              :title="locateHint"
            />
            <!--
              ⚠️ 整张卡片可点击 = 定位到本条风险的段落。role/tabindex 配上键盘处理
              一起加：只给 tabindex 不给快捷键的话，键盘用户能聚焦却激活不了，
              那比不可聚焦更糟。
            -->
            <div
              v-for="risk in workbench.risks"
              :key="risk.risk_id"
              class="risk"
              role="button"
              tabindex="0"
              @click="handleRiskLocate(risk)"
              @keydown.enter.prevent="handleRiskLocate(risk)"
              @keydown.space.prevent="handleRiskLocate(risk)"
            >
              <div class="risk__header">
                <el-tag :type="riskLevelTag(risk.risk_level)" size="small">
                  {{ risk.risk_level }}
                </el-tag>
                <strong class="risk__title">{{ risk.risk_title }}</strong>
                <el-tag size="small" type="info">{{ risk.dimension }}</el-tag>
                <el-tag size="small" type="info">{{ risk.source }}</el-tag>
                <span class="workbench__muted">{{ risk.review_status }}</span>
              </div>

              <dl class="risk__fields">
                <template v-if="risk.reason">
                  <dt>原因</dt>
                  <dd>{{ risk.reason }}</dd>
                </template>
                <template v-if="risk.original_text">
                  <dt>原文片段</dt>
                  <dd class="risk__quote">{{ risk.original_text }}</dd>
                </template>
                <template v-if="risk.legal_basis">
                  <dt>法律依据</dt>
                  <dd>{{ risk.legal_basis }}</dd>
                </template>
                <dt>原文段落</dt>
                <dd>
                  <template v-if="risk.paragraph_index !== null">第 {{ risk.paragraph_index }} 段</template>
                  <template v-else>—</template>
                  <span v-if="risk.clause_id !== null" class="workbench__muted">
                    · 条款 #{{ risk.clause_id }}
                  </span>
                </dd>
              </dl>
            </div>
          </div>
        </el-card>

        <!-- ---------------- 原文 ---------------- -->
        <el-card class="workbench__section" shadow="never">
          <template #header><span>合同原文（{{ workbench.blocks.length }} 段）</span></template>
          <el-empty v-if="workbench.blocks.length === 0" description="暂无原文（该文档没有解析出内容）" />
          <div v-else ref="blocksRef" class="blocks">
            <!--
              ⚠️ data-paragraph-index 是风险定位的坐标：点击风险卡片时用它在原文里
              scrollIntoView + 高亮。**paragraph_index ≠ block_id** —— 前者是段落号
              （P6-2 的位置契约），后者是数据库主键，拿错会跳到不相干的段落。

              is-risk-target 由 activeParagraphIndex 比对得出，不写回 block 对象。
            -->
            <div
              v-for="block in workbench.blocks"
              :key="block.block_id"
              class="block"
              :class="{ 'is-risk-target': activeParagraphIndex === block.paragraph_index }"
              :data-paragraph-index="block.paragraph_index"
            >
              <span class="block__index">[{{ block.paragraph_index }}]</span>
              <span class="block__text">{{ block.text }}</span>
            </div>
          </div>
        </el-card>
      </template>
    </template>
  </div>
</template>

<style scoped>
.workbench__toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.workbench__heading {
  flex: 1 1 auto;
  font-weight: 600;
}

.workbench__muted {
  color: var(--el-text-color-secondary);
  font-weight: normal;
}

.workbench__section {
  margin-bottom: 12px;
}

.workbench__actions {
  margin-top: 12px;
}

.workbench__hash {
  font-family: monospace;
  color: var(--el-text-color-secondary);
}

.clause,
.risk {
  padding: 10px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.clause:last-child,
.risk:last-child {
  border-bottom: none;
}

/* 风险卡片可点击 = 定位到原文对应段落。手型光标是最轻的"这里能点"提示。 */
.risk {
  cursor: pointer;
}

.risk:hover {
  background-color: var(--el-fill-color-light);
}

.risk:focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}

.risks__hint {
  margin-bottom: 8px;
}

.clause__header,
.risk__header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.clause__title {
  color: var(--el-text-color-regular);
}

.clause__text {
  margin-top: 6px;
  white-space: pre-wrap;
  color: var(--el-text-color-regular);
}

.risk__title {
  font-size: 15px;
}

.risk__fields {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: 4px 12px;
  margin: 8px 0 0;
  font-size: 13px;
}

.risk__fields dt {
  color: var(--el-text-color-secondary);
}

.risk__fields dd {
  margin: 0;
}

.risk__quote {
  padding: 2px 6px;
  background-color: var(--el-fill-color-light);
  border-radius: 3px;
}

.blocks {
  font-size: 13px;
  line-height: 1.8;
}

.block {
  display: flex;
  gap: 8px;
}

.block__index {
  flex: 0 0 48px;
  color: var(--el-text-color-placeholder);
  font-family: monospace;
  text-align: right;
}

.block__text {
  white-space: pre-wrap;
}

/*
 * 被定位到的段落。保持到下一次点别的风险为止 —— 不做 setTimeout 自动取消，
 * 也不做动画：用户需要的是"现在指着哪一段"这个稳定事实，不是一个会自己消失的提示。
 */
.block.is-risk-target {
  background-color: var(--el-color-warning-light-9);
  box-shadow: inset 3px 0 0 var(--el-color-warning);
}
</style>
