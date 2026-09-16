/**
 * 人工复核接口（P13-3）。
 *
 * 对应 Backend 的 ``PATCH /api/v1/review-tasks/{task_id}/risks/{risk_id}``（P13-1）。
 *
 * ⚠️ 与 ``workbench.ts`` 分开：那个模块只对应 ``GET .../workbench`` 这一个只读接口，
 * 把"写一条复核结论"塞进去会让"读快照"与"改一行"两件事共用一份文档与一组类型。
 * 后端也是分开的（``services/risk_review.py`` vs ``workbench_query.py``），
 * 前后端的分法保持一致。
 *
 * ⚠️ 请求体**只能**出现三个字段
 * ----------------------------
 * ``review_status`` / ``review_comment`` / ``risk_level``。服务端用
 * ``extra="forbid"`` 挡住了其余一切 —— 传 ``risk_title``、``paragraph_index``
 * 这类 AI 事实字段，或者传 ``reviewer_id`` / ``reviewed_at`` 这类服务端字段，
 * 都会直接 422。这里**不发**它们，不是"后端不要"，而是"本来就不该由调用方决定"。
 */

import { request } from '@/api/request'

/** 复核请求体。字段与 Backend ``RiskReviewRequest`` 一一对应。 */
export interface RiskReviewRequest {
  /** `CONFIRMED` / `REJECTED` / `MODIFIED`。**`PENDING` 会被后端拒绝（422）** */
  review_status: string
  /**
   * 复核意见。
   *
   * ⚠️ 服务端是**整体赋值**（不是"有则改之"）：传 `null` 或不传 = 把意见清空。
   * 因此前端要清空意见时应当显式传 `null`，而不是省略 —— 省略的效果与传 `null` 相同，
   * 但显式写出来能让人看出这是有意为之。
   */
  review_comment?: string | null
  /**
   * 人工修订后的等级。
   * ⚠️ **只在 `review_status === 'MODIFIED'` 时允许出现** ——
   * `CONFIRMED` / `REJECTED` 带上它会被后端 422 拒绝。
   */
  risk_level?: string | null
}

/** 复核结果。字段与 Backend ``RiskReviewResponse`` 一一对应。 */
export interface RiskReviewResult {
  risk_id: number
  task_id: number
  /** 当前生效的风险等级（`MODIFIED` 时是人工修订后的值） */
  risk_level: string
  review_status: string
  review_comment: string | null
  /**
   * 复核人。
   *
   * ⚠️ **恒为 `null`**：项目没有 `sys_user` 表，也没有登录体系，服务端刻意不伪造
   * 一个用户 id。前端**不要**为它编显示值（如"未知用户"）—— 那也是编的。
   */
  reviewer_id: number | null
  /** 复核时刻，**naive UTC**（无 `Z`）。展示前用 `formatUtcTimestamp`。 */
  reviewed_at: string
}

/**
 * 对一条风险写下复核结论。
 *
 * PATCH 本身**幂等**：同样的结论提交两次结果相同，不产生新行，也不需要
 * `Idempotency-Key`。因此 UI 只需禁掉并发点击，不必做去重。
 *
 * :raises ApiError: ``RISK_REVIEW_NOT_READY``(409) 任务未审完；
 *   ``RISK_NOT_FOUND``(404) 风险不存在**或不属于该任务**（后端刻意不区分）；
 *   ``TASK_NOT_FOUND``(404)；422 请求体不合法
 */
export async function reviewRiskItem(
  taskId: number,
  riskId: number,
  payload: RiskReviewRequest,
): Promise<RiskReviewResult> {
  const { data } = await request.patch<RiskReviewResult>(
    `/review-tasks/${taskId}/risks/${riskId}`,
    payload,
  )
  return data
}
