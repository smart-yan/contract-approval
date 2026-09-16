/**
 * 轮询编排（``composables/useReviewPolling``，P14-5-1）。
 *
 * 这里用**假定时器**：轮询的行为主体就是"什么时候再发一次、什么时候不再发"，
 * 真等 1 秒既慢又测不准。用 ``advanceTimersByTimeAsync`` 把时间**推**过去，
 * 顺便让微任务队列跑完（在途的 promise 才会落地）。
 */
import { effectScope } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DEFAULT_POLL_DELAYS_MS, useReviewPolling, type ReviewPolling } from '@/composables/useReviewPolling'

/** 假定时器下等微任务队列跑完（等价于组件测试里的 flushPromises）。 */
async function flush(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0)
}

interface Harness {
  polling: ReviewPolling
  scope: ReturnType<typeof effectScope>
  fetchOnce: ReturnType<typeof vi.fn>
  onData: ReturnType<typeof vi.fn>
  onError: ReturnType<typeof vi.fn>
}

/**
 * 建一个轮询实例。``settledAfter`` 指定"第几次取数返回终态"（null = 永远不终态）。
 *
 * ⚠️ 必须放进 ``effectScope`` 里跑：``onScopeDispose`` 是组件的生命周期钩子，
 * 脱离 scope 调用会告警，而且"卸载后停止"这条也就测不了。
 */
function setup(settledAfter: number | null = null, delaysMs?: readonly number[]): Harness {
  const fetchOnce = vi.fn(async () => ({ n: fetchOnce.mock.calls.length }))
  const onData = vi.fn()
  const onError = vi.fn()

  const scope = effectScope()
  let polling!: ReviewPolling
  scope.run(() => {
    polling = useReviewPolling({
      fetchOnce: fetchOnce as unknown as () => Promise<{ n: number }>,
      isSettled: (data) => settledAfter !== null && data.n >= settledAfter,
      onData,
      onError,
      ...(delaysMs === undefined ? {} : { delaysMs }),
    })
  })

  return { polling, scope, fetchOnce, onData, onError }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useReviewPolling()', () => {
  it('start() 立即取一次，不等第一个间隔', async () => {
    const { polling, fetchOnce } = setup()

    polling.start()
    expect(polling.active.value).toBe(true)
    await flush()

    expect(fetchOnce).toHaveBeenCalledTimes(1)

    polling.stop()
  })

  it('未到终态时按 1s → 2s → 3s → 5s 退避，之后固定 5s', async () => {
    const { polling, fetchOnce } = setup()

    polling.start()
    await flush()
    expect(fetchOnce).toHaveBeenCalledTimes(1) // 立即那次

    // 不到时间不发请求
    await vi.advanceTimersByTimeAsync(999)
    expect(fetchOnce).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(1) // 1s
    expect(fetchOnce).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(2_000) // 2s
    expect(fetchOnce).toHaveBeenCalledTimes(3)

    await vi.advanceTimersByTimeAsync(3_000) // 3s
    expect(fetchOnce).toHaveBeenCalledTimes(4)

    await vi.advanceTimersByTimeAsync(5_000) // 5s
    expect(fetchOnce).toHaveBeenCalledTimes(5)

    await vi.advanceTimersByTimeAsync(5_000) // 封顶后仍是 5s
    expect(fetchOnce).toHaveBeenCalledTimes(6)

    polling.stop()
  })

  it('默认节奏就是 1s/2s/3s/5s', () => {
    expect(DEFAULT_POLL_DELAYS_MS).toEqual([1_000, 2_000, 3_000, 5_000])
  })

  it('取到终态就停：不再发请求，且 active 归 false', async () => {
    const { polling, fetchOnce, onData } = setup(2) // 第二次取数即终态

    polling.start()
    await flush()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(fetchOnce).toHaveBeenCalledTimes(2)
    expect(onData).toHaveBeenCalledTimes(2)
    expect(polling.active.value).toBe(false)

    // 再推 30 秒也不该有第三次
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetchOnce).toHaveBeenCalledTimes(2)
  })

  it('首屏就是终态时一次都不排期（已审完的任务不会继续轮询）', async () => {
    const { polling, fetchOnce } = setup(1)

    polling.start()
    await flush()

    expect(fetchOnce).toHaveBeenCalledTimes(1)
    expect(polling.active.value).toBe(false)

    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetchOnce).toHaveBeenCalledTimes(1)
  })

  it('取数失败 → 立即停止并交错误给 onError（绝不带着错误继续轮询）', async () => {
    const { polling, fetchOnce, onError, onData } = setup()
    const boom = new Error('后端挂了')
    fetchOnce.mockRejectedValueOnce(boom)

    polling.start()
    await flush()
    expect(onError).toHaveBeenCalledWith(boom)
    expect(onData).not.toHaveBeenCalled()
    expect(polling.active.value).toBe(false)

    // 关键：失败之后**不能**还有请求
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetchOnce).toHaveBeenCalledTimes(1)
  })

  it('stop() 清掉待触发的定时器（不留定时器）', async () => {
    const { polling, fetchOnce } = setup()

    polling.start()
    await flush()
    expect(fetchOnce).toHaveBeenCalledTimes(1)

    polling.stop()
    await vi.advanceTimersByTimeAsync(60_000)

    expect(fetchOnce).toHaveBeenCalledTimes(1)
    expect(polling.active.value).toBe(false)
  })

  it('stop() 作废**在途**的那一次：结果不再回调', async () => {
    // 请求已经在飞（还没 resolve），此时卸载 —— 结果回来时不该再渲染
    let release: ((value: { n: number }) => void) | undefined
    const fetchOnce = vi.fn(
      () =>
        new Promise<{ n: number }>((resolve) => {
          release = resolve
        }),
    )
    const onData = vi.fn()
    const scope = effectScope()
    let polling!: ReviewPolling
    scope.run(() => {
      polling = useReviewPolling({ fetchOnce, isSettled: () => true, onData, onError: vi.fn() })
    })

    polling.start()
    await flush()
    polling.stop()

    release?.({ n: 1 })
    await flush()

    expect(onData).not.toHaveBeenCalled()
    scope.stop()
  })

  it('组件卸载（scope 结束）后停止轮询', async () => {
    const { polling, scope, fetchOnce } = setup()

    polling.start()
    await flush()
    expect(fetchOnce).toHaveBeenCalledTimes(1)

    scope.stop() // ≈ 组件卸载
    await vi.advanceTimersByTimeAsync(60_000)

    expect(fetchOnce).toHaveBeenCalledTimes(1)
    expect(polling.active.value).toBe(false)
  })

  it('重复 start() 不会留下两套定时器', async () => {
    const { polling, fetchOnce } = setup()

    polling.start()
    await flush()
    polling.start() // 例如用户手快点了两次"刷新"
    await flush()

    // 两次 start 各立即取一次 = 2 次；但接下来**只有一条**时间线
    expect(fetchOnce).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(1_000)
    expect(fetchOnce).toHaveBeenCalledTimes(3)

    await vi.advanceTimersByTimeAsync(1_000)
    expect(fetchOnce).toHaveBeenCalledTimes(3) // 第二条时间线若还在，这里会变成 4

    polling.stop()
  })
})
