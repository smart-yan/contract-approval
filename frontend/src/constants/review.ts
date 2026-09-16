/**
 * 审查业务常量（P11-5）。
 *
 * ⚠️ 这些是**展示用词表**，不是前端自己发明的状态机：取值必须与 Backend
 * ``app/core/constants.py`` 的 ``TaskStage`` 保持一致。改动时期望两侧一起改 ——
 * 与 Backend 的 `_STAGE_PROGRESS` 是同一条规矩（一处定义，别处引用）。
 *
 * 为什么放 ``constants/`` 而不是页面里：工作台（P11-6）也要显示同一个阶段名，
 * 抄两份迟早会出现"列表页叫已切分、工作台叫已解析"这种同义不同词的漂移。
 */

/** Backend ``TaskStage`` → 中文展示名。 */
export const TASK_STAGE_LABELS: Record<string, string> = {
  UPLOADED: '已上传',
  PARSED: '已解析',
  CLAUSED: '已切分条款',
  REVIEWED: '审查完成',
}

/**
 * 取阶段的中文名；**认不出来的阶段原样返回**。
 *
 * 刻意不兜底成"未知"：真出现了新阶段（后端加了、前端还没跟），
 * 把原始值显示出来才看得出是什么，而"未知"会把线索抹掉。
 */
export function taskStageLabel(stage: string | null | undefined): string {
  if (!stage) {
    return ''
  }
  return TASK_STAGE_LABELS[stage] ?? stage
}

/**
 * 任务在界面上的**三种形态**（P14-5-1）。
 *
 * 判据只有两条，且**全部来自 Backend 已经返回的字段** —— 前端不猜、不推断、
 * 不看时间戳：
 *
 * ================  ==========================================================
 * ``blocked``       ``task.status === 'blocked'`` —— Agent 在后台如实上报的
 *                   「这次跑挂了」（P14-4 的 ``POST .../block``）
 * ``reviewed``      ``task.current_stage === 'REVIEWED'`` —— P9-10 冻结的语义：
 *                   **AI 审查结果已持久化**。⚠️ 它与 ``status`` 无关，
 *                   ``status=pending + current_stage=REVIEWED`` 是**正常组合**
 * ``processing``    其余 —— 图还在后台跑（或还没开始）
 * ================  ==========================================================
 *
 * ⚠️ **``blocked`` 必须排在最前面**：任务被阻塞时 ``current_stage`` 会停在它当时
 * 走到的位置（``CLAUSED`` 甚至 ``UPLOADED``），不会自己跳到 ``REVIEWED``。
 *
 * ⚠️ 为什么**不**拿 ``status`` 单独当进度信号：P10 的裁决是只推阶段、不动状态机，
 * 因此 ``status`` 会长期停在 ``pending``（见 ``api/workbench.ts`` 的说明）。
 * 用它判断"还在处理"，会让一个已经审完的任务永远显示成处理中。
 *
 * 为什么放在 ``constants/`` 而不是 ``api/workbench.ts``：它是**由字段推导出的业务
 * 形态**（词表的一部分），不是 HTTP 层的东西；而且页面测试会把 ``api/workbench``
 * 整个替换成替身，纯函数留在那里会连它一起被替掉。
 */
export type ReviewPhase = 'processing' | 'reviewed' | 'blocked'

/**
 * 由任务字段推导页面形态。
 *
 * 参数写成**结构类型**而不是 ``WorkbenchTask``：本函数只用到两个字段，
 * 收窄入参让它不依赖 API 模块，也便于单测直接喂最小对象。
 */
export function reviewPhase(task: { status: string; current_stage: string }): ReviewPhase {
  if (task.status === 'blocked') {
    return 'blocked'
  }
  if (task.current_stage === 'REVIEWED') {
    return 'reviewed'
  }
  return 'processing'
}

/** Backend ``RiskReviewStatus`` → 中文展示名（P13-3）。 */
export const RISK_REVIEW_STATUS_LABELS: Record<string, string> = {
  PENDING: '待复核',
  CONFIRMED: '已确认',
  REJECTED: '已驳回',
  MODIFIED: '已修改',
}

/**
 * 取复核状态的中文名；**认不出来的原样返回**（与 ``taskStageLabel`` 同一条规矩）。
 */
export function riskReviewStatusLabel(status: string | null | undefined): string {
  if (!status) {
    return ''
  }
  return RISK_REVIEW_STATUS_LABELS[status] ?? status
}

/**
 * 法务**可以选择**的复核结论。
 *
 * ⚠️ 刻意**不含 ``PENDING``**：它是 AI 产出时的初始状态，不是一种"复核结论"。
 * 把它列进选项，等于提供一个"撤销复核"的动作 —— 而库里 `reviewed_at` /
 * `review_comment` 的痕迹**不会**因此消失，用户看到的却是"回到了待复核"。
 * 后端的契约同样拒绝把 `PENDING` 作为目标状态（422），两边口径一致。
 */
export const RISK_REVIEW_DECISIONS = ['CONFIRMED', 'REJECTED', 'MODIFIED'] as const
