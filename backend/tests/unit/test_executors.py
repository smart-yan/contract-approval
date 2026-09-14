"""core/executors.py 单元测试（不需要真实数据库、不真的拉起进程池）。

覆盖：线程池懒加载与单例、受控池大小接管事件循环默认执行器、
OCR 执行器的**懒加载**（P2 期间必须保持未创建）、进程/线程模式选择与降级、关闭语义。
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core import executors
from app.core.config import Settings
from app.core.executors import (
    ensure_ocr_pool,
    executor_status,
    get_ocr_executor,
    get_thread_pool,
    install_default_executor,
    run_blocking,
    shutdown_executors,
)


@pytest.fixture(autouse=True)
def _reset_executors():
    """每个用例前后都清空执行器状态，避免用例之间互相影响。"""
    shutdown_executors()
    yield
    shutdown_executors()


# --------------------------------------------------------------------------- #
# 线程池
# --------------------------------------------------------------------------- #
def test_thread_pool_is_not_created_before_first_use() -> None:
    assert executors._thread_pool is None
    assert executor_status()["thread_pool_created"] is False


def test_thread_pool_is_a_process_wide_singleton() -> None:
    assert get_thread_pool() is get_thread_pool()


def test_shutdown_clears_thread_pool_so_a_new_one_is_built() -> None:
    first = get_thread_pool()
    shutdown_executors()
    assert executors._thread_pool is None

    second = get_thread_pool()
    assert second is not first, "关闭后必须重建，而不是复用已 shutdown 的池"


def test_thread_pool_size_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executors, "get_settings", lambda: Settings(thread_pool_size=3))
    assert get_thread_pool()._max_workers == 3


async def test_run_blocking_executes_off_the_event_loop() -> None:
    """同步阻塞函数必须真的跑在别的线程里，而不是阻塞事件循环。"""
    caller_thread = threading.current_thread().name

    def worker() -> str:
        return threading.current_thread().name

    executed_in = await run_blocking(worker)
    assert executed_in != caller_thread
    assert executed_in.startswith("blocking-io")


async def test_run_blocking_passes_args_and_kwargs() -> None:
    def add(a: int, b: int, *, scale: int = 1) -> int:
        return (a + b) * scale

    assert await run_blocking(add, 1, 2) == 3
    assert await run_blocking(add, 1, 2, scale=10) == 30


async def test_install_default_executor_takes_over_asyncio_to_thread() -> None:
    """接管后 ``asyncio.to_thread`` 必须落到受控线程池（§1.3 的写法得以保持）。

    这里刻意**只做行为断言**，不读 ``loop.get_default_executor()`` ——
    Windows 默认的 ``ProactorEventLoop`` 根本没有这个方法（只有 SelectorEventLoop 有），
    断言内部状态会让测试绑死在平台实现上。
    """
    await install_default_executor()

    executed_in = await asyncio.to_thread(lambda: threading.current_thread().name)
    assert executed_in.startswith("blocking-io"), f"实际执行线程：{executed_in}"


# --------------------------------------------------------------------------- #
# OCR 执行器：懒加载是硬要求
# --------------------------------------------------------------------------- #
def test_ocr_executor_is_not_created_eagerly() -> None:
    """黄金链路只跑 DOCX，P2 期间不得提前拉起 OCR 进程池。"""
    assert get_ocr_executor() is None
    assert executor_status()["ocr_executor_created"] is False


def test_get_ocr_executor_never_triggers_creation() -> None:
    """只读观察接口不能有副作用。"""
    for _ in range(3):
        assert get_ocr_executor() is None
    assert executors._ocr_executor is None


def test_ensure_ocr_pool_is_lazy_and_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        executors, "get_settings", lambda: Settings(ocr_executor_mode="thread", ocr_max_workers=1)
    )
    assert executors._ocr_executor is None, "创建前必须为空"

    first = ensure_ocr_pool()
    assert first is ensure_ocr_pool(), "重复调用必须返回同一个执行器"
    assert isinstance(first, ThreadPoolExecutor)


def test_ensure_ocr_pool_uses_process_pool_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 process 模式：用桩类替换，避免真的 spawn 子进程。"""

    class _FakeProcessPool:
        def __init__(self, max_workers: int | None = None) -> None:
            self.max_workers = max_workers

        def shutdown(self, wait: bool = True) -> None:
            pass

    monkeypatch.setattr(executors, "ProcessPoolExecutor", _FakeProcessPool)
    monkeypatch.setattr(
        executors, "get_settings", lambda: Settings(ocr_executor_mode="process", ocr_max_workers=2)
    )

    pool = ensure_ocr_pool()
    assert isinstance(pool, _FakeProcessPool)
    assert pool.max_workers == 2


def test_process_pool_failure_degrades_to_thread_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """§1.3 硬规则 7：进程池创建失败要降级为线程池，功能不中断。"""

    class _ExplodingProcessPool:
        def __init__(self, max_workers: int | None = None) -> None:
            raise OSError("process pool unavailable")

    monkeypatch.setattr(executors, "ProcessPoolExecutor", _ExplodingProcessPool)
    monkeypatch.setattr(
        executors, "get_settings", lambda: Settings(ocr_executor_mode="process", ocr_max_workers=1)
    )

    pool = ensure_ocr_pool()
    assert isinstance(pool, ThreadPoolExecutor), "创建失败时必须降级为线程池"


def test_ocr_worker_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """OCR_MAX_WORKERS 默认 1，避免 Worker 数 × 进程池大小 造成进程爆炸。"""
    monkeypatch.setattr(
        executors, "get_settings", lambda: Settings(ocr_executor_mode="thread", ocr_max_workers=1)
    )
    assert ensure_ocr_pool()._max_workers == 1


# --------------------------------------------------------------------------- #
# 状态快照与关闭
# --------------------------------------------------------------------------- #
def test_executor_status_reports_config_even_when_nothing_created() -> None:
    status = executor_status()
    assert set(status) == {
        "thread_pool_created",
        "thread_pool_size",
        "ocr_executor_created",
        "ocr_executor_type",
        "ocr_executor_mode",
        "ocr_max_workers",
    }
    assert status["thread_pool_created"] is False
    assert status["ocr_executor_created"] is False
    assert status["ocr_executor_type"] is None


def test_shutdown_is_idempotent() -> None:
    get_thread_pool()
    shutdown_executors()
    shutdown_executors()  # 再关一次不应抛异常
    assert executors._thread_pool is None
    assert executors._ocr_executor is None


def test_shutdown_closes_both_executors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        executors, "get_settings", lambda: Settings(ocr_executor_mode="thread", ocr_max_workers=1)
    )
    get_thread_pool()
    ensure_ocr_pool()
    assert executor_status()["ocr_executor_created"] is True

    shutdown_executors()
    assert executors._thread_pool is None
    assert executors._ocr_executor is None
