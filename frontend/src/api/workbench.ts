/**
 * 审查工作台接口（P11-6）。
 *
 * 对应 Backend 的 ``GET /api/v1/review-tasks/{task_id}/workbench``（P11-4）。
 *
 * ⚠️ 这里的类型**照 Backend 的 response schema 写**（P11-4 的
 * ``app/schemas/workbench.py``），不是照 ORM 推断的。响应里有几处是后端
 * **算出来**的派生字段，前端直接用、不要自己再算：
 *
 * * ``WorkbenchClause.start_paragraph_index`` / ``end_paragraph_index``
 *   ← 后端由 ``start_block_id`` / ``end_block_id`` 换算好的段落号
 * * ``WorkbenchMetadataItem.source_paragraph_index`` ← 同上
 * * ``WorkbenchTask.progress`` ← 后端由 ``current_stage`` 推导
 *
 * 前端**不实现**这些换算：``block_id → paragraph_index`` 是一条数据库结构知识，
 * 抄一份到前端就会两边漂移且不报错。
 */

import { request } from '@/api/request'

/** 本次审查任务。 */
export interface WorkbenchTask {
  task_id: number
  /** 任务状态（Backend constants.TaskStatus）。⚠️ 当前恒为 ``pending``，见下方注释 */
  status: string
  /**
   * 阶段级断点标记（Backend constants.TaskStage）。**这才是当前可用的进度信号**。
   *
   * ⚠️ ``status`` 那条状态机目前没有被推进过（P10 的裁决是只推阶段、不动状态机），
   * 因此界面上不要把 ``status`` 当成"任务进展"来展示。
   */
  current_stage: string
  /** 进度百分比，由后端从 current_stage 推导（前端不重算） */
  progress: number
  /** 入库时刻。**naive UTC**：用 ``formatUtcTimestamp`` 格式化 */
  created_at: string
  /** 结束时刻。当前恒为 null（任务尚未被标记为结束） */
  finished_at: string | null
  /** 综合风险等级（§11.2）。当前恒为 null —— 评分器尚未实现 */
  risk_level_final: string | null
  /** 审查结论（§11.2）。当前恒为 null */
  conclusion: string | null
}

/** 合同主数据。 */
export interface WorkbenchContract {
  contract_id: number
  contract_no: string
  title: string
  contract_type: string
  our_party: string | null
  counterparty: string | null
  /** ⚠️ 后端把 ``Decimal`` 序列化成**字符串**（如 ``"1234.50"``），不是 number */
  amount: string | null
  currency: string | null
  /** 纯日期（``YYYY-MM-DD``），不是时刻 —— 不要当 UTC 时间戳解析 */
  sign_date: string | null
  effective_date: string | null
  expire_date: string | null
  dept: string | null
}

/** 被审查的附件。 */
export interface WorkbenchFile {
  file_id: number
  file_name: string
  /** 文件类型码（DOCX / PDF / …）；由后端从扩展名派生 */
  file_type: string | null
  sha256: string
  parse_status: string
}

/** 一个原文段落块（**文件级**数据）。 */
export interface WorkbenchBlock {
  block_id: number
  /** 全文档线性顺序 —— 原文按它排序渲染 */
  order_index: number
  /** 段落序号（P6-2 的位置契约）。**风险定位就是用这个号** */
  paragraph_index: number
  block_type: string
  text: string
  char_start_global: number
  char_end_global: number
}

/** 一个条款（**任务级**数据）。 */
export interface WorkbenchClause {
  clause_id: number
  clause_no: string | null
  clause_type: string
  title: string | null
  text: string
  /** 后端换算好的段落区间；换算不出来时为 null —— 前端**不要伪造**范围 */
  start_paragraph_index: number | null
  end_paragraph_index: number | null
  start_block_id: number | null
  end_block_id: number | null
}

/** 一条元数据提取项。 */
export interface WorkbenchMetadata {
  field_key: string
  field_label: string
  field_value: string
  value_type: string
  extract_method: string
  source_block_id: number | null
  source_paragraph_index: number | null
}

/** 一条风险。 */
export interface WorkbenchRisk {
  risk_id: number
  /** 规则编码；纯 LLM 风险为 null */
  risk_code: string | null
  risk_title: string
  dimension: string
  /** HIGH / MEDIUM / LOW（Backend constants.RiskLevel） */
  risk_level: string
  /** RULE / LLM / RULE+LLM（Backend constants.RiskSource） */
  source: string
  reason: string | null
  legal_basis: string | null
  /** 命中的原文片段（不是整段原文） */
  original_text: string | null
  /** 命中段落序号 —— 与 ``blocks[].paragraph_index`` 是同一个坐标系，人工对照用 */
  paragraph_index: number | null
  clause_id: number | null
  locator_type: string

  // ---------------------------- 人工复核（P13-2）----------------------------
  // 这四个是**人工判断**，与上面的 AI 产出取自同一行。前端要把两个来源分开呈现：
  // AI 说了什么（risk_title / reason / legal_basis / original_text）不可改，
  // 法务怎么看（下面这四个）才是复核 UI 的写入面。
  /**
   * 人工复核状态（Backend `constants.RiskReviewStatus`）：
   * `PENDING` / `CONFIRMED` / `REJECTED` / `MODIFIED`。
   * AI 刚产出时是 `PENDING`。
   */
  review_status: string
  /**
   * 复核人。
   *
   * ⚠️ **当前恒为 `null`** —— 项目没有 `sys_user` 表、也没有登录/JWT/RBAC，
   * Backend 刻意不伪造一个用户 id 来填它。前端**不要**为它编一个显示值
   * （"未知用户"这类占位同样是编的）。
   */
  reviewer_id: number | null
  /** 复核意见；法务没写时为 null */
  review_comment: string | null
  /**
   * 复核时刻。
   *
   * ⚠️ 与 `task.created_at` 是同一种串：**naive UTC**（没有 `Z`、没有时区偏移）。
   * 展示前请用 `@/utils/datetime` 的 `formatUtcTimestamp`，否则东八区会凭空少 8 小时。
   * 未经复核时为 `null`。
   */
  reviewed_at: string | null
}

/** 工作台的全部数据。 */
export interface WorkbenchResponse {
  task: WorkbenchTask
  contract: WorkbenchContract
  file: WorkbenchFile
  blocks: WorkbenchBlock[]
  clauses: WorkbenchClause[]
  metadata: WorkbenchMetadata[]
  risks: WorkbenchRisk[]
}

/**
 * 取一次审查的完整工作台数据。
 *
 * :raises ApiError: 任务不存在时 ``code === 'TASK_NOT_FOUND'``（404）；
 *   其余为网络/服务端错误。调用方据此区分"任务没了"与"加载失败"。
 */
export async function getReviewTaskWorkbench(taskId: number): Promise<WorkbenchResponse> {
  const { data } = await request.get<WorkbenchResponse>(`/review-tasks/${taskId}/workbench`)
  return data
}
