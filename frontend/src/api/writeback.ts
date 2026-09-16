/**
 * 审批意见回写接口（P15-3b）。
 *
 * 对应 Backend 的 ``POST /api/v1/review-tasks/{task_id}/writeback``。
 *
 * 调用约束
 * --------
 * * **不接受也不发送** ``approval_instance_id`` —— 目标审批单由 Backend 沿
 *   ``ReviewTask → Contract → contract.approval_instance_id`` 解析（§15）。
 *   前端**不能**指定，多传的字段也会被忽略（``test_writeback_execution.py`` 的
 *   ``test_the_api_does_not_accept_an_approval_instance_id`` 钉住）。
 * * **不接受也不发送** ``idempotency_key`` —— 由 Backend 的 renderer 根据
 *   ``task_id + content_md_hash`` 生成，且已写过的同内容会被原样复用。
 * * 请求体**就是空**。Backend 端点签名只接受 ``task_id`` 路径参数。
 *
 * 响应语义
 * --------
 * 返回的是 :class:`WritebackRunResponse`，关键看 ``status`` 字段：
 *
 * ===============  ===========================================
 * ``success``       本地已记录为成功，外部审批系统也已落地一条评论
 * ``failed``        本地已记录为失败 —— caller 可再调一次（FAILED → WRITING 允许）
 * ``writing``       本次调用复用了已有 ``writing`` 记录（重复点击）；**不要重发**
 * ===============  ===========================================
 *
 * ⚠️ HTTP **200** 也可能 ``status=failed``（ApprovalSystemError：网络/超时/5xx），
 * 不是 HTTP 错误。``WRITEBACK_ALREADY_SUCCESS`` 是 **409 HTTP 错误**，两条路径
 * 不能混为一谈。
 */

import { request } from '@/api/request'

/**
 * Backend ``WritebackRunResponse`` 的前端类型。字段名逐字对应。
 *
 * ⚠️ 字段一个不增不减 —— 渲染时不需要 ``approval_instance_id``（前端根本不知道
 * 也不应该知道这条意见会写到哪个审批单），但保留它以便后续排查 / Mock 审批页
 * 跳转能用到。
 */
export interface WritebackResult {
  record_id: number
  task_id: number
  approval_instance_id: number
  /** ``success`` / ``failed`` / ``writing``（后端 §6.2 的状态机字面值） */
  status: string
  /** 含本次在内的已尝试次数 */
  attempt: number
  /** 成功时才有 —— 外部审批系统里的评论 ID */
  external_comment_id: string | null
  /** 失败时才有 —— 人话原因 */
  error_msg: string | null
  /** 结束时刻，**naive UTC**。``null`` 表示还在进行中（仅 ``writing`` 时出现） */
  finished_at: string | null
  /**
   * 本次调用是否真的向审批系统发了写请求。
   * ``false`` 且 ``status=success`` 表示外部早已有这条评论（"响应丢失"恢复），
   * **没有产生第二条评论**。
   */
  posted: boolean
}

/**
 * 把审查意见回写到该任务对应合同关联的审批单。
 *
 * :returns: Backend 返回的写回结果（含 ``status`` / ``attempt`` / ``external_comment_id``
 *   等）。调用方据此决定 UI 提示。
 * :raises ApiError:
 *   * ``TASK_NOT_FOUND``(404) —— 任务不存在
 *   * ``CONTRACT_NOT_FOUND``(404) —— 合同不存在
 *   * ``NOT_FOUND``(404) —— 审批单行不存在（曾经关联过、后来审批单没了）
 *   * ``WRITEBACK_NOT_READY``(409) —— 任务尚未审查完成
 *   * ``WRITEBACK_APPROVAL_NOT_READY``(409) —— 合同尚未关联审批单
 *   * ``WRITEBACK_ALREADY_SUCCESS``(409) —— 同一内容已成功回写
 *   * 网络 / 后端不可达 → ``code`` 回落 ``NETWORK_ERROR`` / ``REQUEST_TIMEOUT``
 *
 * ⚠️ **业务错误（4xx）不会被吞掉**：Backend 把外部实现的业务性失败以 ``AppError``
 * 形式重新抛出（例如外部审批单行不存在的 404）；这里的拦截器把它转成 ``ApiError``，
 * 调用方应当按 409 / 404 区分提示，不当作"写回成功"。
 */
export async function postWriteback(taskId: number): Promise<WritebackResult> {
  const { data } = await request.post<WritebackResult>(`/review-tasks/${taskId}/writeback`)
  return data
}
