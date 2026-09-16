<script setup lang="ts">
/**
 * 审查工作台（P11-6 骨架 + P11-7 风险定位 + P13-3 人工复核）。
 *
 * 一次请求拿齐渲染所需的全部数据：``GET /api/v1/review-tasks/{taskId}/workbench``。
 * 页面把它分成几块展示：任务 → 合同 → 文件 → 元数据 → 条款 → 风险 → 原文。
 *
 * 风险定位（P11-7）：点风险卡片 → 读 ``risk.paragraph_index`` → 找原文里同号的
 * ``[data-paragraph-index]`` → 滚动 + 高亮。**前端只做这一跳**：``paragraph_index``
 * 是 P10 冻结的定位坐标，语义定位（quote → 段落）是 Agent 的职责（P9），
 * 在这里重做一遍等于把同一条契约实现两次。
 *
 * 人工复核（P13-3）：每条风险卡片上写一条复核结论（确认 / 驳回 / 修改等级）。
 * ``PATCH .../risks/{riskId}`` 成功后**只就地更新那一条**，不重新拉整个工作台 ——
 * 重拉会把用户在其他卡片上还没保存的输入冲掉，也白白多跑 5 条 SELECT。
 *
 * ⚠️ 数据**只读**：页面不修改 API 返回的任何对象（不在 risk / block 上挂
 * ``highlighted`` / ``active`` / ``review_status`` 之类的状态）。选中状态、复核草稿、
 * 保存结果**一律另开 ref 维护**，模板按"本地覆盖优先"读取。
 * 那份响应是一次快照，被就地改写后就再也说不清"原始数据是什么样"。
 *
 * 审查中轮询（P14-5-1）：上传后跳进来时任务可能还在后台跑（P14-4 起受理即返回 202），
 * 因此本页在任务未到终态时**反复取同一份数据**，直到 ``current_stage === 'REVIEWED'``
 * 或被 ``status === 'blocked'`` 拦下。判据只有这两条，全部来自 Backend
 * （见 ``constants/review.ts`` 的 ``reviewPhase``）；页面**不自己算进度、不自己超时**。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/request'
import { reviewRiskItem, type RiskReviewRequest, type RiskReviewResult } from '@/api/risk-review'
import { getReviewTaskWorkbench, type WorkbenchResponse, type WorkbenchRisk } from '@/api/workbench'
import { postWriteback, type WritebackResult } from '@/api/writeback'
import { useReviewPolling } from '@/composables/useReviewPolling'
import {
  RISK_REVIEW_DECISIONS,
  riskReviewStatusLabel,
  reviewPhase,
  taskStageLabel,
  type ReviewPhase,
} from '@/constants/review'
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

// --------------------------------------------------------------------------- //
// 人工复核（P13-3）—— 全部是**页面自己的状态**，不写回 API 返回的对象
// --------------------------------------------------------------------------- //
/** 复核表单的草稿（按 risk_id 索引）。加载后为每条风险初始化一份。 */
interface ReviewDraft {
  /** `''` = 还没选。**不会是 `PENDING`** —— 它不是可选的复核结论 */
  review_status: string
  review_comment: string
  /** 人工风险等级的候选值；未选 MODIFIED 时只是原样带着，不参与提交 */
  risk_level: string
}

/** 服务端确认过的复核结果（按 risk_id 索引）。保存成功后才有条目。 */
interface SavedReview {
  review_status: string
  review_comment: string | null
  risk_level: string
  reviewed_at: string
  reviewer_id: number | null
}

const reviewDrafts = ref<Record<number, ReviewDraft>>({})
const savedReviews = ref<Record<number, SavedReview>>({})
/** 正在保存的 risk_id。用来禁按钮，防止连点产生重复请求。 */
const savingRiskIds = ref<number[]>([])
/** 每条风险最近一次保存失败的原因（成功后清空）。 */
const reviewErrors = ref<Record<number, string>>({})

/**
 * 新建一条草稿时的初值。
 *
 * ⚠️ 当前状态是 ``PENDING`` 时，**下拉框留空**（``''``）而不是预选 ``PENDING`` ——
 * 后者不在选项里，预选一个"选不出来的值"只会让用户以为已经选好了。
 * 已知的三种已复核状态则照原样预选，方便用户直接改（复核过的风险仍可再改）。
 */
function draftFrom(risk: WorkbenchRisk): ReviewDraft {
  return {
    review_status: risk.review_status === 'PENDING' ? '' : risk.review_status,
    review_comment: risk.review_comment ?? '',
    risk_level: risk.risk_level,
  }
}

/** 取某条风险的草稿；没有就现建一份（模板因此永远拿得到值）。 */
function draftOf(risk: WorkbenchRisk): ReviewDraft {
  return reviewDrafts.value[risk.risk_id] ?? draftFrom(risk)
}

function setDraft(riskId: number, risk: WorkbenchRisk, patch: Partial<ReviewDraft>): void {
  reviewDrafts.value = { ...reviewDrafts.value, [riskId]: { ...draftOf(risk), ...patch } }
}

/** 加载/刷新后重建草稿 —— 服务端数据变了，旧草稿就没有意义了。 */
function seedReviewDrafts(data: WorkbenchResponse): void {
  const drafts: Record<number, ReviewDraft> = {}
  for (const risk of data.risks) {
    drafts[risk.risk_id] = draftFrom(risk)
  }
  reviewDrafts.value = drafts
  savedReviews.value = {}
  reviewErrors.value = {}
}

/** 当前**已保存**的复核结论 —— 卡片头部用它，草稿改动不提前反映到状态标签上。 */
function savedOf(risk: WorkbenchRisk): SavedReview | null {
  return savedReviews.value[risk.risk_id] ?? null
}

function reviewStatusOf(risk: WorkbenchRisk): string {
  return savedOf(risk)?.review_status ?? risk.review_status
}

function reviewedAtOf(risk: WorkbenchRisk): string | null {
  return savedOf(risk)?.reviewed_at ?? risk.reviewed_at
}

/** 当前**生效**的风险等级：保存过 MODIFIED 之后，库里那一列已经是人工值。 */
function riskLevelOf(risk: WorkbenchRisk): string {
  return savedOf(risk)?.risk_level ?? risk.risk_level
}

function isSaving(risk: WorkbenchRisk): boolean {
  return savingRiskIds.value.includes(risk.risk_id)
}

function reviewErrorOf(risk: WorkbenchRisk): string {
  return reviewErrors.value[risk.risk_id] ?? ''
}

function reviewStatusTag(status: string): 'success' | 'danger' | 'warning' | 'info' {
  switch (status) {
    case 'CONFIRMED':
      return 'success'
    case 'REJECTED':
      return 'danger'
    case 'MODIFIED':
      return 'warning'
    default:
      return 'info'
  }
}

/**
 * 把复核失败翻译成一句人话。
 *
 * ⚠️ **422 单独判 ``status`` 而不是 ``code``**：后端的请求体校验错误走的是
 * FastAPI 默认处理器，响应体是 ``{"detail": [...]}``，**没有** ``code`` 字段 ——
 * 拦截器会把它归成 ``NETWORK_ERROR``（见 ``api/request.ts`` 的回退分支）。
 * 若照 ``code`` 走，用户会看到"网络错误"，而真实原因是"参数不合法"。
 * 代价是**后端那句精确的校验文案拿不到**（它只存在于 ``detail`` 里）；
 * 这一层取舍已在 P13-3 报告中记录，等契约统一后再改成直接展示后端文案。
 */
function reviewErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return '复核失败，请稍后重试'
  }
  switch (error.code) {
    case 'RISK_REVIEW_NOT_READY':
      return '任务尚未完成风险审查，当前无法复核'
    case 'RISK_NOT_FOUND':
      return '这条风险不存在，或不属于当前任务'
    case 'TASK_NOT_FOUND':
      return '审查任务不存在'
    default:
      break
  }
  if (error.status === 422) {
    return '提交的复核内容不合法（请检查复核结论与风险等级的搭配）'
  }
  return error.message
}

/**
 * 提交一条复核结论。
 *
 * 请求体**只**由草稿里的三个字段拼成 —— AI 事实字段（``risk_title`` / ``reason`` /
 * ``original_text`` / ``paragraph_index`` …）与服务端字段（``reviewer_id`` /
 * ``reviewed_at``）一律不发，后端也会拒绝。
 *
 * 成功后**不重新拉工作台**，只用返回值更新这一条 —— 重拉会冲掉其他卡片上
 * 还没保存的输入。
 */
async function saveReview(risk: WorkbenchRisk): Promise<void> {
  const id = taskId.value
  const draft = draftOf(risk)
  if (id === null || isSaving(risk)) {
    return
  }
  if (!draft.review_status) {
    reviewErrors.value = { ...reviewErrors.value, [risk.risk_id]: '请先选择复核结论' }
    return
  }

  const payload: RiskReviewRequest = {
    review_status: draft.review_status,
    // 显式传 null 表示"清空意见" —— 服务端是整体赋值，省略与 null 等价
    review_comment: draft.review_comment.trim() === '' ? null : draft.review_comment.trim(),
  }
  // ⚠️ 只有 MODIFIED 才能带等级：另两种结论带上它会被后端 422 拒绝
  if (draft.review_status === 'MODIFIED') {
    payload.risk_level = draft.risk_level
  }

  savingRiskIds.value = [...savingRiskIds.value, risk.risk_id]
  reviewErrors.value = { ...reviewErrors.value, [risk.risk_id]: '' }

  try {
    const result: RiskReviewResult = await reviewRiskItem(id, risk.risk_id, payload)
    savedReviews.value = {
      ...savedReviews.value,
      [risk.risk_id]: {
        review_status: result.review_status,
        review_comment: result.review_comment,
        risk_level: result.risk_level,
        reviewed_at: result.reviewed_at,
        reviewer_id: result.reviewer_id,
      },
    }
    // 草稿与服务端对齐（服务端可能规范化过文本），并让"已保存"成为新的编辑起点
    reviewDrafts.value = {
      ...reviewDrafts.value,
      [risk.risk_id]: {
        review_status: result.review_status,
        review_comment: result.review_comment ?? '',
        risk_level: result.risk_level,
      },
    }
  } catch (error) {
    // ⚠️ 失败时**保留用户输入**（草稿不动），只记一条提示
    reviewErrors.value = { ...reviewErrors.value, [risk.risk_id]: reviewErrorMessage(error) }
  } finally {
    savingRiskIds.value = savingRiskIds.value.filter((value) => value !== risk.risk_id)
  }
}

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

/**
 * 人工可选的风险等级。
 *
 * ⚠️ **从上面那张表派生**，不另立一份等级清单：两处各写一遍的话，
 * 将来加一档等级时总会漏掉一处，而漏掉的那处**不会报错**。
 */
const RISK_LEVELS = Object.keys(RISK_LEVEL_TAG)

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

/**
 * 取一次工作台数据。
 *
 * ⚠️ ``taskId`` 为 null 时**抛一个 TASK_NOT_FOUND**，而不是返回空数据：调用方
 * （下面的轮询）只有"拿到数据"与"出错"两条路，"参数不合法"属于后者，交给既有的
 * 错误分支显示成"任务不存在"即可 —— 与 P11 的原有处理一致。
 */
async function fetchWorkbench(): Promise<WorkbenchResponse> {
  const id = taskId.value
  if (id === null) {
    // 只有 taskId 合法才会启动轮询，这里正常到不了
    throw new ApiError({ code: 'TASK_NOT_FOUND', message: '任务 ID 不合法' })
  }
  return await getReviewTaskWorkbench(id)
}

/**
 * 轮询（P14-5-1）：任务没到终态就继续问 Backend，到了就停。
 *
 * 停的三种情况（见 ``composables/useReviewPolling``）：终态 / 取数失败 / 组件卸载。
 *
 * ⚠️ 每次轮询都会重建复核草稿（``seedReviewDrafts``）—— 这是**安全的**，因为
 * 轮询只在任务还没审完时进行，那时 ``risks`` 必然为空、用户也无从填写草稿；
 * 一旦转到 ``REVIEWED``，这一轮就是最后一轮，之后不再有请求覆盖用户的输入。
 */
const polling = useReviewPolling<WorkbenchResponse>({
  fetchOnce: fetchWorkbench,
  isSettled: (data) => reviewPhase(data.task) !== 'processing',
  onData: (data) => {
    workbench.value = data
    loading.value = false
    seedReviewDrafts(data)
  },
  onError: (error) => {
    workbench.value = null
    loading.value = false
    if (error instanceof ApiError && error.code === 'TASK_NOT_FOUND') {
      notFound.value = true
    } else {
      // ⚠️ 不静默失败，也**不显示成"暂无数据"** —— 「没拿到数据」与「没有数据」
      // 是两件事，混淆会让人以为合同真的没内容。
      // 轮询在错误上**停止**（不会一边报错一边继续请求），页面给一个"重试"入口。
      errorMessage.value = error instanceof ApiError ? error.message : '加载审查工作台失败'
    }
  },
})

/** 页面的任务形态。数据还没到时按"处理中"渲染（首屏就是等待状态）。 */
const phase = computed<ReviewPhase>(() =>
  workbench.value === null ? 'processing' : reviewPhase(workbench.value.task),
)

/**
 * 顶部横幅的说明文字（未到终态时才显示，见模板）。
 *
 * ⚠️ 阻塞原因**只展示 ``block_reason_msg``**（上报方写的人话，P14-5-2 起由工作台
 * 接口透出）。前端**不翻译** ``block_reason_code`` —— 把 ``UNSUPPORTED_FORMAT``
 * 映射成一句中文，等于在前端再维护一份后端词表，两边迟早对不上且不会报错。
 *
 * 原因缺失时给的是**明确的兜底话术**（"服务端未提供具体原因"），而不是空串或
 * ``null``：让用户以为"没有原因"与让用户看到一句半截的话，都不如如实说明。
 */
const phaseHint = computed<string>(() => {
  const task = workbench.value?.task
  if (task === undefined) {
    return '正在读取审查状态……'
  }
  if (phase.value === 'blocked') {
    // ``?.`` 同时兜住两种"没有原因"：接口明确给了 null，以及旧版接口压根没这个键
    const reason = task.block_reason_msg?.trim()
    return reason ? `原因：${reason}` : '审查已阻塞，服务端未提供具体原因'
  }
  return `当前阶段：${taskStageLabel(task.current_stage)}（进度 ${task.progress}%）。审查在后台进行，完成后本页会自动展示结果。`
})

/**
 * 加载 / 刷新：立即取一次，任务没结束就继续轮询到结束为止。
 * 首屏、工具栏的"刷新"、错误卡片上的"重试"都走这里，语义只有这一个。
 */
function load(): void {
  errorMessage.value = ''
  notFound.value = false
  loading.value = true

  const id = taskId.value
  if (id === null) {
    // 参数不合法 —— 不发请求，也不伪装成"加载失败"
    notFound.value = true
    loading.value = false
    return
  }

  polling.start()
}

function backToContracts(): void {
  // 显式导航，不依赖浏览器 history —— 从别处直接打开本页时 history 里可能没有上一页
  void router.push('/contracts')
}

// --------------------------------------------------------------------------- //
// 审批回写（P16-2）—— 操作状态 / 错误展示，不改 API 返回的对象
// --------------------------------------------------------------------------- //
//
/**
 * 写回按钮是否在跑：用于按钮 ``loading`` + 防并发点击。
 *
 * ⚠️ 写回请求本身**可能耗时**（要调外部审批系统），UI 不能让用户重复点；
 * ``loading=true`` 时按钮 disable，由 try/finally 保证必然回落。
 */
const writebackLoading = ref(false)

/**
 * 写回结果快照（成功 / 失败）。``null`` = 还没回写过。
 *
 * ⚠️ **保留跨刷新**：写回结果在任务级别是有意义的事实，不能因为 workbench
 * 重新加载（轮询、点刷新）就清掉 —— 那等于"成功写过、但刷新之后按钮又出现"，
 * 用户会以为刚才那次没生效。所以这里**不**与 ``workbench.value`` 绑定，
 * ``onData`` 不重置它，只在重新发起写回时按状态机更新。
 */
const writebackResult = ref<WritebackResult | null>(null)

/**
 * 写回操作本身抛出的业务错误（4xx / 网络）。与 :data:`writebackResult.error_msg`
 * 是两件事：
 *
 * * ``writebackResult.error_msg`` ← Backend 在 ``status=failed`` 时给的人话原因
 * * ``writebackError`` ← 前端把 4xx / 网络错误转成一句业务提示
 *
 * 409 ``WRITEBACK_ALREADY_SUCCESS`` **不进这里**：那是"已经成功过"的事实，会
 * 直接把 :data:`writebackResult` 标成 success（见 :func:`submitWriteback`）。
 */
const writebackError = ref('')

/**
 * 写回操作的错误翻译（仿 :func:`reviewErrorMessage` 的写法）。
 *
 * ⚠️ **422 单独判 ``status`` 而不是 ``code``**：与 P13-3 同一条理由 —— FastAPI
 * 默认 422 校验响应里没有 ``code``，拦截器会归成 ``NETWORK_ERROR``。
 */
function writebackErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return '回写失败，请稍后重试'
  }
  switch (error.code) {
    case 'WRITEBACK_NOT_READY':
      return '该任务尚未审查完成，无法回写'
    case 'WRITEBACK_APPROVAL_NOT_READY':
      return '该合同尚未关联审批单，无法回写审批意见'
    case 'TASK_NOT_FOUND':
      return '审查任务不存在'
    case 'CONTRACT_NOT_FOUND':
      return '审查任务关联的合同不存在'
    case 'NOT_FOUND':
      return '合同关联的审批单行不存在'
    default:
      break
  }
  if (error.status === 422) {
    return '请求参数不合法'
  }
  return error.message
}

/**
 * 提交一次写回（首次 / 重试都走这里）。
 *
 * 重要语义
 * --------
 * * **不接 ``approval_instance_id``** —— Backend 沿合同侧解析（§15）。
 * * **不接 ``idempotency_key``** —— Backend 的 renderer 按
 *   ``task_id + content_md_hash`` 生成；同内容第二次提交会被原样复用，不产生
 *   第二条评论（§6.2 冻结）。
 * * **409 ``WRITEBACK_ALREADY_SUCCESS`` 不当作错误**：那是"已经成功过"的
 *   事实，把 :data:`writebackResult` 标成 success，UI 进入"已写回"态，
 *   与正常成功路径**视觉上一致**。
 * * **不要重发**：见 ``writeback_result.status === 'success'`` 的按钮 disabled
 *   逻辑（模板）。
 *
 * ⚠️ **只在 ``phase === 'reviewed'`` 时调用**：调用方（按钮）负责门禁，这里
 * 不再二次校验 —— 二次校验要么写在按钮的 ``disabled``、要么写在这里一份，结果
 * 是同一件事多写一遍。
 */
async function submitWriteback(): Promise<void> {
  const id = taskId.value
  if (id === null || writebackLoading.value) {
    return
  }

  writebackLoading.value = true
  writebackError.value = ''

  try {
    const result = await postWriteback(id)
    writebackResult.value = result
  } catch (error) {
    if (error instanceof ApiError && error.code === 'WRITEBACK_ALREADY_SUCCESS') {
      // 409 表达的语义是"已经成功了" —— 与正常成功路径一致处理：
      // 1. 把结果标成 success（attempt 至少为 1，但前端拿不到历史记录，用 attempt=1 占位）
      // 2. **不**当作错误展示
      writebackResult.value = writebackResult.value ?? {
        record_id: 0,
        task_id: id,
        approval_instance_id: 0,
        status: 'success',
        attempt: 1,
        external_comment_id: null,
        error_msg: null,
        finished_at: null,
        posted: false,
      }
      return
    }
    writebackError.value = writebackErrorMessage(error)
  } finally {
    writebackLoading.value = false
  }
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

      <!--
        任务还没走到终态时的横幅（P14-5-1）。两种形态互斥：
          · processing —— 图还在后台跑，页面每 1/2/3/5 秒问一次 Backend
          · blocked    —— Agent 如实上报"这次跑挂了"，轮询已停
        ⚠️ **没有** "status=pending 就是失败" 这种判断：pending 与 REVIEWED 并存
        是 P9-10 冻结的正常组合，那是 reviewed 形态，不在这里显示。
      -->
      <el-alert
        v-if="workbench && phase !== 'reviewed'"
        class="workbench__phase"
        :type="phase === 'blocked' ? 'error' : 'info'"
        show-icon
        :closable="false"
        :title="phase === 'blocked' ? '审查任务已阻塞' : 'AI 正在审查中'"
        :description="phaseHint"
      />

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
                <el-tag :type="riskLevelTag(riskLevelOf(risk))" size="small">
                  {{ riskLevelOf(risk) }}
                </el-tag>
                <strong class="risk__title">{{ risk.risk_title }}</strong>
                <el-tag size="small" type="info">{{ risk.dimension }}</el-tag>
                <el-tag size="small" type="info">{{ risk.source }}</el-tag>
                <el-tag size="small" :type="reviewStatusTag(reviewStatusOf(risk))">
                  {{ riskReviewStatusLabel(reviewStatusOf(risk)) }}
                </el-tag>
                <!-- 原始状态码一并留着：与报告里的「审查完成（REVIEWED）」同一写法 -->
                <span class="workbench__muted">{{ reviewStatusOf(risk) }}</span>
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

              <!--
                ---------- 人工复核（P13-3） ----------
                ⚠️ @click.stop / @keydown.stop 是**必须**的：整张卡片是"点击定位原文"
                的 role=button，不拦住冒泡的话，点下拉框会顺带把原文滚走，
                在意见框里按回车会直接触发定位。
              -->
              <div class="review" @click.stop @keydown.stop>
                <div class="review__head">
                  <span class="review__label">人工复核</span>
                  <template v-if="reviewedAtOf(risk)">
                    <span class="workbench__muted">
                      复核于 {{ formatUtcTimestamp(reviewedAtOf(risk)) }}
                    </span>
                  </template>
                  <template v-else>
                    <span class="workbench__muted">尚未复核</span>
                  </template>
                </div>

                <div class="review__form">
                  <el-select
                    class="review__status"
                    :model-value="draftOf(risk).review_status"
                    placeholder="选择复核结论"
                    size="small"
                    :disabled="isSaving(risk)"
                    @update:model-value="(value: unknown) => setDraft(risk.risk_id, risk, { review_status: String(value) })"
                  >
                    <!-- 选项来自 RISK_REVIEW_DECISIONS：**没有 PENDING** -->
                    <el-option
                      v-for="decision in RISK_REVIEW_DECISIONS"
                      :key="decision"
                      :label="riskReviewStatusLabel(decision)"
                      :value="decision"
                    />
                  </el-select>

                  <!-- 人工等级只在 MODIFIED 出现；AI 的等级仍在卡片上方，只读 -->
                  <el-select
                    v-if="draftOf(risk).review_status === 'MODIFIED'"
                    class="review__level"
                    :model-value="draftOf(risk).risk_level"
                    placeholder="人工风险等级"
                    size="small"
                    :disabled="isSaving(risk)"
                    @update:model-value="(value: unknown) => setDraft(risk.risk_id, risk, { risk_level: String(value) })"
                  >
                    <el-option v-for="level in RISK_LEVELS" :key="level" :label="level" :value="level" />
                  </el-select>

                  <el-input
                    class="review__comment"
                    type="textarea"
                    :rows="2"
                    size="small"
                    placeholder="复核意见（可选）"
                    :model-value="draftOf(risk).review_comment"
                    :disabled="isSaving(risk)"
                    @update:model-value="(value: string) => setDraft(risk.risk_id, risk, { review_comment: value })"
                  />

                  <el-button
                    type="primary"
                    size="small"
                    :loading="isSaving(risk)"
                    :disabled="isSaving(risk) || !draftOf(risk).review_status"
                    @click="saveReview(risk)"
                  >
                    保存复核
                  </el-button>
                </div>

                <el-alert
                  v-if="reviewErrorOf(risk)"
                  class="review__error"
                  type="error"
                  :closable="false"
                  show-icon
                  :title="reviewErrorOf(risk)"
                />
              </div>
            </div>
          </div>
        </el-card>

        <!-- ---------------- 审批回写（P16-2 / P15-3b） ---------------- -->
        <!--
          ⚠️ **只**在 ``phase === 'reviewed'`` 时渲染这张卡片：写回门禁是
          ``current_stage == REVIEWED``（Backend §15-2 冻结）。processing / blocked
          时连卡片都不显示，用户就不会误以为可以写回。

          这张卡片与下面"合同原文"是平级关系，不属于某个风险卡片 —— 它表达的是
          "对这份审查整体"的操作。
        -->
        <el-card v-if="phase === 'reviewed'" class="workbench__section" shadow="never">
          <template #header><span>审批回写</span></template>

          <!--
            三种结果形态互斥：success / failed / 未写过。互斥由 v-if / v-else-if 链
            保证 —— 不会出现"同时显示成功和失败"的诡异组合。
          -->
          <div v-if="writebackResult?.status === 'success'" class="workbench__writeback">
            <el-alert
              type="success"
              :closable="false"
              show-icon
              :title="writebackResult.posted
                ? '审查意见已成功回写到审批单'
                : '回写已完成（外部审批系统已有该意见，本次未重复发送）'"
            />
            <dl class="workbench__writeback-fields">
              <template v-if="writebackResult.external_comment_id">
                <dt>外部评论 ID</dt>
                <dd><code>{{ writebackResult.external_comment_id }}</code></dd>
              </template>
              <dt>尝试次数</dt>
              <dd>第 {{ writebackResult.attempt }} 次</dd>
              <dt>完成时刻</dt>
              <dd>{{ writebackResult.finished_at ? formatUtcTimestamp(writebackResult.finished_at) : '—' }}</dd>
            </dl>
            <!--
              SUCCESS 后按钮保持可见但 **disabled** —— 不消失，让用户能看出"这就是写
              回操作的最终态"；再次点击也不会重发（Backend 自身也用 409 挡）。
            -->
            <div class="workbench__actions">
              <el-button :disabled="true">已成功回写</el-button>
            </div>
          </div>

          <div v-else-if="writebackResult?.status === 'failed'" class="workbench__writeback">
            <el-alert
              type="error"
              :closable="false"
              show-icon
              title="回写失败"
              :description="writebackResult.error_msg ?? '审批系统写入失败'"
            />
            <dl class="workbench__writeback-fields">
              <dt>已尝试</dt>
              <dd>{{ writebackResult.attempt }} 次</dd>
              <dt>完成时刻</dt>
              <dd>{{ writebackResult.finished_at ? formatUtcTimestamp(writebackResult.finished_at) : '—' }}</dd>
            </dl>
            <!--
              失败可以重试：FAILED → WRITING 在状态机里允许（Backend §6.2）。
              不重置 ``writebackResult``，而是**覆盖**它（见 ``submitWriteback``）。
            -->
            <div class="workbench__actions">
              <el-button type="primary" :loading="writebackLoading" @click="submitWriteback">
                重试写回
              </el-button>
            </div>
          </div>

          <div v-else class="workbench__writeback">
            <p class="workbench__writeback-hint">
              将本任务的审查意见发送到该合同关联的审批单评论区。同内容重复提交不会产生第二条评论。
            </p>
            <div class="workbench__actions">
              <el-button type="primary" :loading="writebackLoading" @click="submitWriteback">
                回写审批意见
              </el-button>
            </div>
          </div>

          <!--
            业务错误（4xx / 网络）展示在卡片最下方 —— 与 success/failed 卡片互不重叠。
            success / failed 时不显示这个 alert。
          -->
          <el-alert
            v-if="writebackError"
            class="workbench__writeback-error"
            type="error"
            :closable="false"
            show-icon
            :title="writebackError"
          />
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

/* 审查中 / 已阻塞的横幅（P14-5-1）：与下面各张卡片拉开一点距离 */
.workbench__phase {
  margin-bottom: 12px;
}

/* ---------------- 人工复核（P13-3） ---------------- */
/* 与只读的 AI 事实用一条分隔线隔开：上面是"AI 说了什么"，下面是"法务怎么看"。 */
.review {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--el-border-color-lighter);
}

.review__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 13px;
}

.review__label {
  color: var(--el-text-color-secondary);
}

.review__form {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  flex-wrap: wrap;
}

.review__status {
  width: 140px;
}

.review__level {
  width: 120px;
}

.review__comment {
  flex: 1 1 220px;
  min-width: 180px;
}

.review__error {
  margin-top: 8px;
}

/* ---------------- 审批回写（P16-2）---------------- */
.workbench__writeback {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.workbench__writeback-hint {
  margin: 0;
  color: var(--el-text-color-regular);
  font-size: 13px;
  line-height: 1.6;
}

.workbench__writeback-fields {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 4px 12px;
  margin: 0;
  font-size: 13px;
}

.workbench__writeback-fields dt {
  color: var(--el-text-color-secondary);
}

.workbench__writeback-fields dd {
  margin: 0;
}

.workbench__writeback-fields code {
  font-family: monospace;
  background-color: var(--el-fill-color-light);
  padding: 0 4px;
  border-radius: 3px;
}

.workbench__writeback-error {
  margin-top: 8px;
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
