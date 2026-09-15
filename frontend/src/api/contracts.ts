/**
 * 合同查询接口（P11-5）。
 *
 * 对应 Backend 的 ``GET /api/v1/contracts``（P11-3）。
 *
 * ⚠️ 这里的类型**照着 Backend 的 response schema 写**，不是照 ORM Model 推断的。
 * 两者不等价：响应里没有 ``contract.status`` / ``contract.current_task_id``
 * （那两个字段在 Backend 侧已经过期、接口刻意不暴露），前端因此**根本不知道它们存在** ——
 * 这是有意的，避免有人拿它们当状态来源。
 */

import { request } from '@/api/request'

/** 合同下**最新一条**审查任务的摘要。 */
export interface ReviewTaskSummary {
  /** 审查任务 ID —— 进工作台要用它 */
  task_id: number
  /** 任务状态，见 Backend constants.TaskStatus */
  status: string
  /** 阶段级断点标记，见 Backend constants.TaskStage。**这才是可用的进度信号** */
  current_stage: string
  /** 进度百分比（0~100），由 Backend 从 current_stage 推导 */
  progress: number
}

/** 合同列表的一项。 */
export interface ContractListItem {
  contract_id: number
  contract_no: string
  title: string
  contract_type: string
  /**
   * 入库时刻。
   *
   * ⚠️ **naive UTC**：后端统一存 UTC，JSON 里因此是 ``2026-09-15T10:00:00`` ——
   * **没有 ``Z``、也没有时区偏移**。直接 `new Date(它)` 会被按本地时区解析，
   * 东八区会凭空少 8 小时。展示前请用 `@/utils/datetime` 的 `formatUtcTimestamp`。
   */
  created_at: string
  /** 该合同下 id 最大的审查任务；**为 null 表示还没发起过审查**（不伪造默认任务） */
  latest_task: ReviewTaskSummary | null
}

/**
 * 取回全部合同（按创建时间倒序）。
 *
 * 接口**不分页**：MVP 的合同量很小，加分页只会带来分页器与"翻页时数据变了"的成本。
 * 返回空数组是正常结论（"暂无合同"），不是错误。
 */
export async function getContracts(): Promise<ContractListItem[]> {
  const { data } = await request.get<ContractListItem[]>('/contracts')
  return data
}
