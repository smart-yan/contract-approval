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
