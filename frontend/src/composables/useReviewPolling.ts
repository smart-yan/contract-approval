/**
 * “这次审查跑完了没有”的轮询（P14-5-1）。
 *
 * 为什么需要它
 * ----------
 * P14-4 起 ``POST /api/agent/review`` 只返回 **202 + task_id**：受理了，图在 Agent
 * 进程里跑。于是"审查完了没有"只能靠**问 Backend** —— 事实来源是 Backend 的
 * ``ReviewTask``，前端不自己维护第二套进度（P14-4 的裁决）。
 *
 * 于是本模块只做一件事：**按退避节奏反复取工作台，直到任务落到终态为止**。
 *
 * 刻意不做的（都属于后续阶段）
 * --------------------------
 * * **不做 SSE / WebSocket**：为一个 10 秒级的后台任务引入长连接，代价（重连、
 *   代理、部署）远大于收益
 * * **不做进度百分比推算**：进度是 Backend 从 ``current_stage`` 推出的
 *   （见 ``api/workbench.ts``），前端重算就是第二份实现
 * * **不做任务状态缓存**：每次轮询就是一次真实的 ``GET``，拿到什么显示什么
 *
 * 什么时候**停**（三条，缺一不可）
 * ------------------------------
 * 1. ``isSettled`` 判为终态 → 正常收工
 * 2. 取数**失败** → 停止并交给 ``onError``。⚠️ 绝不能"失败了继续轮询"：
 *    后端挂了的时候，那样会变成每 5 秒一次的无限请求风暴，而且用户永远看不到原因
 * 3. 组件卸载 / 显式 ``stop()`` → 清掉定时器。⚠️ 在途的那一次取数结果会被**丢弃**
 *    （generation 守卫），否则"停止之后又渲染了一次"会让调用方以为还活着
 *
 * 节奏
 * ----
 * 首次**立即**取一次（用户刚上传完，等待感最强），随后按 ``delaysMs`` 退避，
 * 用完后固定用最后一个间隔。这是一个"够用且能解释"的策略 —— 不是自适应算法，
 * 也没有尝试次数上限：页面开着盯一个进行中的任务时，轮询本身就是用户要的；
 * 真要收工，走上面那三条。
 */

import { onScopeDispose, readonly, ref, type Ref } from 'vue'

/** 退避节奏（毫秒）：1s → 2s → 3s → 5s，之后一直是 5s。 */
export const DEFAULT_POLL_DELAYS_MS: readonly number[] = [1_000, 2_000, 3_000, 5_000]

export interface ReviewPollingOptions<T> {
  /** 取一次数据。抛出的异常即视为"这次轮询失败"。 */
  fetchOnce: () => Promise<T>
  /** 这份数据是否已经到终态（到了就停止轮询）。 */
  isSettled: (data: T) => boolean
  /** 每取到一份数据（含最后一次）。 */
  onData: (data: T) => void
  /** 取数失败。调用后轮询即停止，由调用方负责展示。 */
  onError: (error: unknown) => void
  /** 覆盖退避节奏（测试用；默认见 :data:`DEFAULT_POLL_DELAYS_MS`）。 */
  delaysMs?: readonly number[]
}

export interface ReviewPolling {
  /** 是否处于"还在轮询"状态（供界面显示"审查中"用）。 */
  active: Readonly<Ref<boolean>>
  /** 立即取一次；未到终态就继续按节奏轮询。重复调用会先停掉上一轮。 */
  start: () => void
  /** 停止轮询并作废在途的那一次。幂等。 */
  stop: () => void
}

export function useReviewPolling<T>(options: ReviewPollingOptions<T>): ReviewPolling {
  const delays = options.delaysMs ?? DEFAULT_POLL_DELAYS_MS
  const active = ref(false)

  let timer: ReturnType<typeof setTimeout> | null = null
  /**
   * 每一次"开始轮询"占用一个编号。异步返回时编号对不上，说明中间已经被
   * ``stop()`` / 重新 ``start()`` 过，这次结果一律丢弃。
   * 有它才敢在卸载时清定时器：**在途的请求是取消不掉的**，只能选择不认账。
   */
  let generation = 0
  let attempts = 0

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  function stop(): void {
    generation += 1
    clearTimer()
    active.value = false
  }

  function scheduleNext(): void {
    // ``attempts`` 是**已完成**的取数次数；刚跑完第一次 → 用 delays[0]。
    // 超出表长后固定用最后一个间隔：长跑时不该无限拉长（那样界面上什么都看不到了）。
    const index = Math.min(Math.max(attempts - 1, 0), delays.length - 1)
    const delay = delays[index] ?? 0
    timer = setTimeout(() => {
      void run()
    }, delay)
  }

  async function run(): Promise<void> {
    const mine = generation
    // ⚠️ 定时器已经触发，句柄必须就地清掉 —— 留着它的话 stop() 会去 clear 一个
    // 早已触发的句柄（无害但说不清状态），而且"有没有待触发的定时器"就不准了
    timer = null
    attempts += 1

    let data: T
    try {
      data = await options.fetchOnce()
    } catch (error) {
      if (mine !== generation) {
        return
      }
      // 失败即停：绝不带着错误继续轮询（见模块 docstring 第 2 条）
      active.value = false
      options.onError(error)
      return
    }

    if (mine !== generation) {
      return
    }

    options.onData(data)

    if (options.isSettled(data)) {
      active.value = false
      return
    }

    scheduleNext()
  }

  function start(): void {
    stop() // 重复 start 不该留下两套定时器
    attempts = 0
    active.value = true
    void run() // 首次立即取，不等第一个间隔
  }

  // 组件卸载（或 effect scope 结束）时收工。少了这一句，定时器会活过组件本身，
  // 卸载后还在发请求 —— 那是最典型的一种定时器泄漏。
  onScopeDispose(stop)

  return { active: readonly(active), start, stop }
}
