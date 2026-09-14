"""并发执行器基座（架构文档 §1.3）。

三档并发模型
------------
==============  ==========================  ==========================================
类别             执行方式                     说明
==============  ==========================  ==========================================
原生 async       ``await``（不经过本模块）     LLM HTTP、数据库读写等 I/O 密集操作
同步阻塞 I/O     ``run_blocking()`` /         python-docx、PyMuPDF 等无 async 版本的
                 ``asyncio.to_thread()``      C 扩展，必须离开事件循环
CPU / Native 密集 ``ensure_ocr_pool()``        OCR 推理、图像预处理等秒级 CPU 任务
==============  ==========================  ==========================================

两条设计原则
------------
1. **懒加载**：本模块在 import 期、以及应用启动期都**不创建任何执行器**。
   尤其 OCR 进程池 —— 黄金链路当前只跑 DOCX，提前拉起一个永不使用的进程池
   会白白占用内存并加载模型。只有第一次真正调用 ``ensure_ocr_pool()`` 时才创建。

2. **受控的线程池大小**：``asyncio.to_thread()`` 默认使用事件循环的默认执行器
   （CPython 里是 ``min(32, cpu+4)`` 个线程），无法通过配置约束。
   ``install_default_executor()`` 在启动时把默认执行器换成
   ``THREAD_POOL_SIZE`` 指定大小的线程池，于是
   **既保留了 ``asyncio.to_thread()`` 的写法（架构文档 §1.3 的示例代码形状不变），
   又让池大小受配置控制**。

Windows 注意事项
----------------
``ProcessPoolExecutor`` 在 Windows 上使用 spawn 启动子进程，要求入口模块有
``if __name__ == "__main__":`` 保护，且被提交的函数必须可 pickle（模块级函数，不能是
闭包/lambda）。P7b 实现 OCR 时需遵守这两条。
"""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------- #
# 模块级状态：执行器实例（懒加载，进程内各一份）
# --------------------------------------------------------------------------- #
_thread_pool: ThreadPoolExecutor | None = None
_ocr_executor: Executor | None = None
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 线程池：同步阻塞 I/O
# --------------------------------------------------------------------------- #
def get_thread_pool() -> ThreadPoolExecutor:
    """返回同步阻塞 I/O 用的线程池（懒加载、进程内单例）。"""
    global _thread_pool
    if _thread_pool is None:
        with _lock:
            if _thread_pool is None:
                size = get_settings().thread_pool_size
                _thread_pool = ThreadPoolExecutor(
                    max_workers=size,
                    thread_name_prefix="blocking-io",
                )
                logger.info("阻塞 I/O 线程池已创建", extra={"thread_pool_size": size})
    return _thread_pool


async def install_default_executor() -> None:
    """把事件循环的默认执行器换成受控线程池。**应在应用启动时调用一次**。

    之后 ``await asyncio.to_thread(fn, ...)`` 就会使用 ``THREAD_POOL_SIZE``
    指定大小的池，而不是 CPython 的默认大小。
    """
    loop = asyncio.get_running_loop()
    loop.set_default_executor(get_thread_pool())
    logger.debug("事件循环默认执行器已接管", extra={"thread_pool_size": get_settings().thread_pool_size})


async def run_blocking[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """在受控线程池中执行同步阻塞函数，返回其结果。

    与 ``asyncio.to_thread()`` 等价，区别是**显式指定**线程池 ——
    在默认执行器已被替换的前提下两者行为一致；此函数用于需要明确表达意图的场合。

    用法::

        blocks = await run_blocking(parser.parse, file.storage_path)
    """
    loop = asyncio.get_running_loop()
    if kwargs:
        call = functools.partial(fn, *args, **kwargs)
        return await loop.run_in_executor(get_thread_pool(), call)
    return await loop.run_in_executor(get_thread_pool(), fn, *args)


# --------------------------------------------------------------------------- #
# OCR 执行器：CPU / Native 密集（懒加载）
# --------------------------------------------------------------------------- #
def ensure_ocr_pool() -> Executor:
    """返回 OCR 执行器，**首次调用时才创建**（双检锁保证只创建一次）。

    模式由 ``OCR_EXECUTOR_MODE`` 决定：

    * ``process``（默认）—— 独立进程池，避免秒级 CPU 计算与事件循环调度争用；
      失败时自动降级为线程池（§1.3 硬规则 7），并记 warn 日志。
    * ``thread`` —— 线程池（实测更优时使用）。

    ⚠️ 黄金链路只跑 DOCX，**P2 期间不会有任何代码调用本函数**，
    因此不会提前拉起进程池。
    """
    global _ocr_executor
    if _ocr_executor is None:
        with _lock:
            if _ocr_executor is None:
                _ocr_executor = _create_ocr_executor()
    return _ocr_executor


def _create_ocr_executor() -> Executor:
    settings = get_settings()
    workers = settings.ocr_max_workers

    if settings.ocr_executor_mode == "process":
        try:
            pool = ProcessPoolExecutor(max_workers=workers)
        except Exception as exc:  # noqa: BLE001 - 受限环境下降级，功能不中断
            logger.warning(
                "OCR 进程池创建失败，降级为线程池",
                extra={"error_type": type(exc).__name__, "workers": workers},
            )
        else:
            logger.info("OCR 进程池已创建", extra={"mode": "process", "workers": workers})
            return pool

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr")
    logger.info("OCR 线程池已创建", extra={"mode": "thread", "workers": workers})
    return pool


def get_ocr_executor() -> Executor | None:
    """返回**已创建**的 OCR 执行器；未创建时返回 ``None``。

    与 ``ensure_ocr_pool()`` 的区别：本函数**不会触发创建**，
    供 ``/health`` 等只读场景观察懒加载是否按预期工作。
    """
    return _ocr_executor


# --------------------------------------------------------------------------- #
# 状态快照与关闭
# --------------------------------------------------------------------------- #
def executor_status() -> dict[str, Any]:
    """执行器状态快照（供 ``/health`` 使用，只读、不触发创建）。"""
    settings = get_settings()
    return {
        "thread_pool_created": _thread_pool is not None,
        "thread_pool_size": settings.thread_pool_size,
        "ocr_executor_created": _ocr_executor is not None,
        "ocr_executor_type": type(_ocr_executor).__name__ if _ocr_executor else None,
        "ocr_executor_mode": settings.ocr_executor_mode,
        "ocr_max_workers": settings.ocr_max_workers,
    }


def shutdown_executors(*, wait: bool = True) -> None:
    """关闭全部执行器并清空模块状态。**应用关闭时调用**。

    调用后再次 ``get_thread_pool()`` / ``ensure_ocr_pool()`` 会重新创建新实例，
    因此测试之间可以安全地反复启动/关闭。
    """
    global _thread_pool, _ocr_executor
    with _lock:
        if _ocr_executor is not None:
            _ocr_executor.shutdown(wait=wait)
            logger.info("OCR 执行器已关闭", extra={"ocr_executor_type": type(_ocr_executor).__name__})
            _ocr_executor = None
        if _thread_pool is not None:
            _thread_pool.shutdown(wait=wait)
            logger.info("阻塞 I/O 线程池已关闭")
            _thread_pool = None
