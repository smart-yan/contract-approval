"""后台任务登记处的生命周期（P14-4，不需要 MySQL / 不需要起应用）。

P14-4 把图的执行搬到了 HTTP 请求之外，于是"谁在跑、跑完没跑完、炸了谁知道"
必须有人回答 —— 这个模块就是那些答案。这里逐条钉住：

* 任务被登记、被看见
* 结束时**无论成败**都从登记处消失（否则集合只涨不落）
* 异常**不被静默吞掉**
* 并发闸门真的挡得住
* ``drain`` 会等，等不到会取消
"""

from __future__ import annotations

import asyncio
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
