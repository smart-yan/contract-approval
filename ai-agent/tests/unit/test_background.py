"""后台任务登记处的生命周期（P14-4，不需要 MySQL / 不需要起应用）。

P14-4 把图的执行搬到了 HTTP 请求之外，于是"谁在跑、跑完没跑完、炸了谁知道"
必须有人回答 —— 这个模块就是那些答案。这里逐条钉住：

* 任务被登记、被看见
* 结束时**无论成败**都从登记处消失（否则集合只涨不落）
* 异常**不被静默吞掉**
* 并发闸门真的挡得住
* ``drain`` 会等，等不到会取消
* **排队时就被取消**的任务：协程体从没跑过，它的 ``finally`` 也不会跑 ——
  资源由 ``on_abandon`` 兜住，且不留下 "never awaited" 噪声
"""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from app.background import BackgroundReviews


async def _ok(value: int = 1) -> int:
    return value


async def _boom() -> None:
    raise ValueError("图炸了")


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


# --------------------------------------------------------------------------- #
# 1：登记与清理
# --------------------------------------------------------------------------- #
async def test_a_started_task_is_visible_while_running() -> None:
    registry = BackgroundReviews()
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    registry.start(work(), name="review:1")

    assert registry.active_count == 1
    assert registry.active_names() == ["review:1"]

    gate.set()
    await asyncio.gather(*list(registry._tasks))

    assert registry.active_count == 0, "跑完必须从登记处消失，否则集合只涨不落"


async def test_a_finished_task_is_removed() -> None:
    registry = BackgroundReviews()

    task = registry.start(_ok(7), name="review:2")
    await task

    # done callback 是在任务结束后由事件循环调度的，给它一次机会
    await asyncio.sleep(0)
    assert registry.active_count == 0


async def test_a_failed_task_is_also_removed() -> None:
    """**失败也要落账** —— 否则一个总是失败的任务会把名额永久占住。"""
    registry = BackgroundReviews()

    task = registry.start(_boom(), name="review:3")
    with pytest.raises(ValueError):
        await task

    await asyncio.sleep(0)
    assert registry.active_count == 0


async def test_several_tasks_are_tracked_independently() -> None:
    registry = BackgroundReviews()
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    for index in range(3):
        registry.start(work(), name=f"review:{index}")

    assert registry.active_count == 3
    assert registry.active_names() == ["review:0", "review:1", "review:2"]

    gate.set()
    await asyncio.gather(*list(registry._tasks))
    assert registry.active_count == 0


# --------------------------------------------------------------------------- #
# 2：异常不被静默吞掉
# --------------------------------------------------------------------------- #
async def test_an_uncaught_exception_is_logged_not_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**本文件最重要的一条。**

    业务协程理应自己兜住异常（它还要调 Backend 上报），真漏到登记处说明有 bug ——
    那时也必须留下 ``ERROR`` 级日志，绝不能静默消失。
    """
    registry = BackgroundReviews()

    with caplog.at_level(logging.ERROR, logger="app.background"):
        task = registry.start(_boom(), name="review:boom")
        with pytest.raises(ValueError):
            await task
        await asyncio.sleep(0)

    assert any("未捕获异常" in record.message for record in caplog.records), (
        f"漏到登记处的异常必须记日志，实际：{[r.message for r in caplog.records]}"
    )


async def test_a_cancelled_task_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """被取消（例如 shutdown 超时）同样要留痕 —— "哪些没跑完"必须查得到。"""
    registry = BackgroundReviews()

    with caplog.at_level(logging.WARNING, logger="app.background"):
        task = registry.start(_sleep(10), name="review:cancel")
        await asyncio.sleep(0)  # 让协程真的开始跑
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    assert any("被取消" in record.message for record in caplog.records)


# --------------------------------------------------------------------------- #
# 3：并发闸门
# --------------------------------------------------------------------------- #
async def test_the_semaphore_caps_concurrent_execution() -> None:
    """``start()`` 立刻返回，但**同时**跑的数量受闸门限制，超出的排队。

    这是"一个请求风暴不能把几百张图同时压进内存"那条约束的落点。
    """
    registry = BackgroundReviews(concurrency=2)
    running = 0
    peak = 0
    release = asyncio.Event()

    async def work() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1

    tasks = [registry.start(work(), name=f"review:{i}") for i in range(5)]
    await asyncio.sleep(0.05)  # 让拿到名额的先跑起来

    assert peak == 2, f"同时最多跑 2 个，实际峰值 {peak}"

    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2


async def test_start_returns_immediately_even_when_saturated() -> None:
    """满额时 ``start()`` **不许**阻塞 —— 否则请求会被挂住，退回同步模型。"""
    registry = BackgroundReviews(concurrency=1)
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    registry.start(work(), name="review:0")
    await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    registry.start(work(), name="review:1")  # 这一下必须立刻返回
    elapsed = loop.time() - started

    assert elapsed < 0.05, f"start() 被挂住了 {elapsed:.3f}s"
    assert registry.active_count == 2, "排队中的任务同样在登记处里（它还没跑完）"

    release.set()
    await asyncio.gather(*list(registry._tasks))


# --------------------------------------------------------------------------- #
# 4：drain
# --------------------------------------------------------------------------- #
async def test_drain_waits_for_running_tasks() -> None:
    registry = BackgroundReviews()
    done: list[str] = []

    async def work() -> None:
        await asyncio.sleep(0.02)
        done.append("ok")

    registry.start(work(), name="review:0")
    await registry.drain()

    assert done == ["ok"], "drain 必须等到任务真的跑完"
    assert registry.active_count == 0


async def test_drain_on_an_empty_registry_returns_at_once() -> None:
    await BackgroundReviews().drain()  # 不该挂住、也不该报错


async def test_drain_cancels_tasks_that_outlive_the_timeout() -> None:
    """关服务不该被一个慢任务无限期拖住 —— 超时就取消，**并且记下日志**。"""
    registry = BackgroundReviews(drain_timeout_seconds=0.05)
    cancelled = False

    async def work() -> None:
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise

    registry.start(work(), name="review:slow")
    await asyncio.sleep(0)

    await registry.drain()

    assert cancelled, "超时的任务必须被取消"
    assert registry.active_count == 0, "取消之后同样要从登记处消失"


async def test_drain_does_not_raise_when_a_task_failed() -> None:
    """失败的任务不该让 ``drain`` 抛出去 —— 关闭流程必须能走完。"""
    registry = BackgroundReviews()
    task = registry.start(_boom(), name="review:boom")
    await asyncio.sleep(0)

    await registry.drain()  # 不抛

    assert task.done()
    assert registry.active_count == 0


# --------------------------------------------------------------------------- #
# 5：排队中被放弃 —— 协程体没跑过，收尾只能由 on_abandon 兜
# --------------------------------------------------------------------------- #
async def test_a_queued_task_gives_its_resources_back_when_abandoned() -> None:
    """**闸门外的取消是另一条路径**：``coro`` 一次都没被 ``await`` 过。

    它的 ``try/finally`` 因此完全没有机会执行 —— 这正是 ``on_abandon`` 存在的理由。
    这里用一个"释放资源"的替身，确认它**确实被调了**；真实资源（临时上传文件）
    由端点级用例覆盖。
    """
    registry = BackgroundReviews(concurrency=1, drain_timeout_seconds=0.05)
    gate = asyncio.Event()
    released: list[str] = []

    async def release() -> None:
        released.append("on_abandon")

    async def holder() -> None:
        await gate.wait()

    async def queued() -> None:
        released.append("协程体")  # 不该出现：它排在闸门外，没轮到就被取消了
        await asyncio.sleep(10)

    registry.start(holder(), name="review:holder")
    await asyncio.sleep(0)  # 让占位任务真的占住唯一的名额

    registry.start(queued(), name="review:queued", on_abandon=release)
    await asyncio.sleep(0)
    assert registry.active_count == 2, "排队中的任务同样在登记处里"

    await registry.drain()

    assert released == ["on_abandon"], "协程体没跑，收尾只能由 on_abandon 完成"
    assert registry.active_count == 0


async def test_an_abandoned_coroutine_is_closed_not_left_unawaited() -> None:
    """被放弃的协程必须**显式关闭**。

    没被 ``await`` 过的协程对象被回收时，Python 会报 "coroutine ... was never
    awaited"。那条警告在这条路径上**是误报**：真正发生的是"这次审查没开始"，
    不是"有人写漏了 await" —— 它出现在日志里只会把排查引到错的方向。

    ⚠️ 这里断言的是**协程对象自身的状态**，不是"警告有没有出现"：警告在回收时
    才发出，测试里靠抓警告来断言是不稳定的（实测过：不 ``close()`` 时警告会溜到
    测试的警告汇总里，而断言照样通过）。状态是确定的 —— ``close()`` 过就是
    ``CORO_CLOSED``，没被碰过就是 ``CORO_CREATED``。
    """

    async def queued() -> None:
        await asyncio.sleep(10)  # pragma: no cover - 永远不会执行到这里

    registry = BackgroundReviews(concurrency=1, drain_timeout_seconds=0.05)

    async def holder() -> None:
        await asyncio.Event().wait()

    registry.start(holder(), name="review:holder")
    await asyncio.sleep(0)

    coro = queued()
    assert inspect.getcoroutinestate(coro) == "CORO_CREATED", "还没人 await 过它"

    registry.start(coro, name="review:queued")
    await asyncio.sleep(0)
    await registry.drain()

    assert inspect.getcoroutinestate(coro) == "CORO_CLOSED", (
        "放弃时必须 close()，否则回收时会报一条误导性的 never awaited"
    )
