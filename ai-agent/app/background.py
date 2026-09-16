"""进程内后台任务登记处（P14-4）。

它解决什么
---------
P14-4 起 ``POST /api/agent/review`` **不再等待**整张图跑完：请求阶段只做
预上传与取规则集，拿到 ``task_id`` 就返回 202，图放到后台执行。

一旦有了"请求之外还在跑的东西"，就必须回答五个问题，本模块就是答案：

============================  ====================================================
谁在跑                        一个**:class:`asyncio.Task` 集合**
跑飞了谁管                    ``_on_done`` 回调 —— 被取消或有未捕获异常时**记日志**，
                              不让它静默消失
太多怎么办                    :class:`asyncio.Semaphore` 限制**同时**跑图的数量；
                              超出的在进程内排队（请求照常立刻返回 202）
排队时被取消怎么办            **协程体没跑过，它自己的 ``finally`` 也就没机会跑** ——
                              因此 ``on_abandon`` 回调：调用方登记前分配的资源，
                              由调用方自己释放（见下）
进程关闭怎么办                :meth:`drain` —— 等一小会儿，还跑不完就取消
============================  ====================================================

为什么需要 ``on_abandon``
----------------------
闸门在建任务时**不 await**（否则满额时请求会被挂住），因此额度是任务**内部**才拿到的：
协程要么先排队、要么直接开始跑。问题出在排队那一段 —— 此时业务协程**一次都没被
``await`` 过**，它的 ``try/finally`` 自然不会执行。于是"登记这个任务之前就分配好的
资源"（例如请求阶段落盘的临时上传文件）会**没人释放**，同时 Python 还会抱怨
``coroutine ... was never awaited``。

本模块不知道那些资源是什么（也不该知道），只保证**该通知的时候一定通知**：
任务在拿到额度之前就被取消 → 调一次 ``on_abandon``。协程**已经跑起来**之后的取消
走它自己的 ``finally``，与这里无关。

为什么不用 FastAPI 的 ``BackgroundTasks``
--------------------------------------
它的语义是"**响应发出之后**执行"，但它**不提供**：任务句柄、并发上限、
完成回调、以及在 shutdown 时等待/取消的能力。对"一次 HTTP 请求触发一段
分钟级后台工作"来说，缺的恰好都是必需的。``asyncio.create_task`` 直接得多，
代价只是上面四件事要自己写 —— 就写在下面。

⚠️ 已知边界（**刻意的**，见 P14-4 报告）
--------------------------------------
* **进程重启 = 未完成的后台任务丢失**。没有跨进程队列、没有 checkpoint
  （LangGraph 的 checkpointer 明确不做）。任务会停在中间阶段，等人处理。
  排队中被放弃的任务连图都没开始跑，因此它**不会被标成 blocked**（没有"失败"可言），
  但 ``on_abandon`` 保证它的临时文件不会留在磁盘上
* 并发上限只约束**单进程**。生产级调度（多副本、抢占、重试退避）属后续阶段
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

#: 单进程内**同时**执行图的上限。
#:
#: 为什么是 4：一次审查的 CPU 大头是 DOCX 解析（走线程池）与等 LLM 的网络 IO，
#: 进程本身很轻。4 是一个"够用且能解释"的值 —— 它防的是"一个请求风暴把
#: 几百张图同时压进内存"，不是精确的容量规划。
#: ⚠️ 这是**单进程 MVP** 的取舍：真正的容量控制属于任务队列的范畴，
#: 而当前项目明确不引入队列（§17.6：用 MySQL 表当队列是 P14 之后的事）。
DEFAULT_CONCURRENCY = 4

#: shutdown 时等待在跑任务收尾的上限（秒）。超时就取消 ——
#: 关服务不该被一个慢任务无限期拖住。
DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0


class BackgroundReviews:
    """一张图的后台任务登记处。**每个进程一个**，挂在 ``app.state`` 上。"""

    def __init__(
        self,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._drain_timeout = drain_timeout_seconds
        self._concurrency = concurrency

    # ----------------------------------------------------------------- #
    # 生命周期
    # ----------------------------------------------------------------- #
    def start(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
        on_abandon: Callable[[], Awaitable[None]] | None = None,
    ) -> asyncio.Task[Any]:
        """把一段协程放到后台跑，返回它的 Task。

        ⚠️ 调用方**不要** ``await`` 返回值 —— 这个方法的全部意义就是"不等它"。
        返回值只用于测试与排查（例如断言它确实被启动了）。

        协程的**并发闸门在协程内部**（见 :meth:`_run`），不在这里：如果在
        ``start`` 里就 ``await semaphore.acquire()``，满额时**请求会被挂住**，
        那就退回同步模型了。

        :param on_abandon: 任务**在拿到额度之前**就被取消时调用一次 ——
            那时 ``coro`` 一次都没被 ``await`` 过，它自己的 ``finally`` 不会执行。
            调用方在这里释放"登记之前就已经分配好"的资源（例如临时上传文件）。
            已经排到额度、跑起来的任务走它自己的 ``finally``，**不会**调这个回调。
            默认 ``None`` = 该任务没有这种资源。
        """
        task = asyncio.create_task(self._run(coro, on_abandon=on_abandon, name=name), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._on_done)
        logger.info("后台审查已登记 | name=%s 在跑=%d", name, len(self._tasks))
        return task

    async def drain(self) -> None:
        """shutdown 时等还在跑的任务收尾；超时则取消。

        **不吞掉任何东西**：超时被取消的任务由 :meth:`_on_done` 记下日志，
        "哪些没跑完"这件事在服务关闭时是必须能查到的。
        """
        if not self._tasks:
            return

        logger.info("等待后台审查收尾 | 在跑=%d", len(self._tasks))
        _done, pending = await asyncio.wait(self._tasks, timeout=self._drain_timeout)

        for task in pending:
            logger.warning("后台审查超时未完成，取消 | name=%s", task.get_name())
            task.cancel()

        if pending:
            # 给取消一个真正落地的机会，否则事件循环关闭时会再报一次
            await asyncio.gather(*pending, return_exceptions=True)

    # ----------------------------------------------------------------- #
    # 内省（测试与排查用）
    # ----------------------------------------------------------------- #
    @property
    def active_count(self) -> int:
        return len(self._tasks)

    @property
    def concurrency(self) -> int:
        return self._concurrency

    def active_names(self) -> list[str]:
        return sorted(task.get_name() for task in self._tasks)

    # ----------------------------------------------------------------- #
    # 内部
    # ----------------------------------------------------------------- #
    async def _run(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        on_abandon: Callable[[], Awaitable[None]] | None,
        name: str,
    ) -> Any:
        """在并发闸门内执行协程。

        闸门放在这里而不是 :meth:`start`：``start`` 必须**立刻返回**，
        否则满额时请求会被挂住。超额的协程在这里排队，等待一次可用的额度。

        ⚠️ 闸门**不能**用 ``async with`` 一把包住 —— 那样"排队时被取消"与
        "执行时被取消"会挤进同一个 ``except``，而两者要处理的事情完全不同：
        前者连 ``coro`` 都没开始（它自己的 ``finally`` 不会跑），后者已经跑过了。
        拆成两段之后，每段各自负责自己要收的尾。
        """
        try:
            await self._semaphore.acquire()
        except asyncio.CancelledError:
            # 还在排队就被取消：业务协程一次都没被 await 过 —— 交给 on_abandon
            await self._abandon(coro, on_abandon=on_abandon, name=name)
            raise

        try:
            return await coro
        finally:
            self._semaphore.release()

    async def _abandon(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        on_abandon: Callable[[], Awaitable[None]] | None,
        name: str,
    ) -> None:
        """协程**从未启动**就被取消时的收尾（见 :meth:`start` 的 ``on_abandon``）。

        ``coro.close()`` 不是可有可无的：没被 await 过的协程对象被回收时，
        Python 会报 ``coroutine ... was never awaited`` —— 而这条路径上真正发生的
        是"这次审查没开始"，不是"有 bug"，不该留下一条看起来像 bug 的噪声。

        ⚠️ 清理失败只记日志、**不抛**：调用点正在处理 ``CancelledError``，
        这里再抛会把它盖掉，于是"被取消"变成"清理失败"，真正的结局反而看不出来。
        """
        coro.close()

        if on_abandon is None:
            return

        try:
            await on_abandon()
        except Exception:
            logger.exception("后台审查被放弃时的清理失败 | name=%s", name)
        else:
            logger.warning("后台审查排队阶段被放弃，已释放其资源 | name=%s", name)

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        """任务结束时的兜底日志。

        ⚠️ 这里**不是**业务异常的处理点（那个在后台协程自己的 ``except`` 里，
        因为它要调 Backend 上报）。这里只兜住两种"没人接"的情况：

        * **被取消**：shutdown 超时或显式取消
        * **未捕获异常**：业务协程理应自己兜住，真漏到这里说明有 bug ——
          记 ``exception`` 级别的日志，绝不静默

        刻意**不重新抛出**：这个回调在事件循环里跑，抛出去只会变成
        "Task exception was never retrieved" 的噪声，信息还不如这里记的日志全。
        """
        if task.cancelled():
            logger.warning("后台审查被取消 | name=%s", task.get_name())
            return

        exc = task.exception()
        if exc is not None:
            logger.exception(
                "后台审查有未捕获异常（业务协程本应自己兜住）| name=%s",
                task.get_name(),
                exc_info=exc,
            )


__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "BackgroundReviews",
]
