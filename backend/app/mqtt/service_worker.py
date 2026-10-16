"""把 MQTT 订阅服务放进**独立线程**运行。

为什么不用 FastAPI 的事件循环
----------------------------
aiomqtt 要求 **SelectorEventLoop**（原因见 `core/asyncio_compat.py`），
而 uvicorn 在 Windows 上：

| 启动方式 | 用的循环 | 订阅服务能否内嵌 |
|---|---|---|
| `uvicorn app.main:app` | 系统默认 = **ProactorEventLoop** | ❌ |
| `uvicorn --reload` | uvicorn **显式设置** ProactorEventLoop | ❌ |

更要命的是"事件循环一旦创建就不能改类型"，所以内嵌方案在 Windows 上没有干净的解法。

**独立线程是更解耦的答案**：

1. 线程里自己建循环、自己设 policy，与 uvicorn 用什么循环**完全无关** —— `--reload` 也能用；
2. 落库是同步阻塞的（走 `to_thread`），跑在独立线程里不会干扰 API 的响应时间；
3. 仍然在**同一个进程**内，因此**共享同一份设备目录与最新值缓存** —— API 能直接读到实时值，
   不需要引入 Redis、也不需要跨进程通信。

代价只有一个：缓存变成跨线程共享，所以 `LatestValueCache` 内部加了锁。

停止逻辑
--------
只用 "Event 通知" 不够：订阅循环大部分时间挂在 `async for message in client.messages` 上等新消息，
没有新消息就不会回来检查标志位。所以停止时**从外部取消整个任务**（`call_soon_threadsafe`），
让 `run()` 的 `finally` 走完收尾（它会做最后一次 flush）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time

from app.core.asyncio_compat import ensure_selector_loop_on_windows
from app.mqtt.subscriber import MqttIngestService

log = logging.getLogger("app.mqtt.worker")


class MqttWorker:
    """在独立线程里跑 `MqttIngestService`。

    用法：
        worker = MqttWorker(service)
        worker.start()          # 非阻塞，立刻返回
        ...
        worker.stop()           # 请求停止并等待线程收尾
    """

    def __init__(self, service: MqttIngestService, *, stop_timeout: float = 8.0):
        self.service = service
        self.stop_timeout = stop_timeout
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.started_at: float | None = None
        self.error: str | None = None

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self.is_alive:
            return
        self.error = None
        self._thread = threading.Thread(target=self._run, name="mqtt-ingest", daemon=True)
        self._thread.start()
        self.started_at = time.time()

    def _run(self) -> None:
        # ⚠️ 在【新线程】里设 policy 是有效的：policy 只影响"之后新建的循环"，
        # 主线程那个已经建好的 uvicorn 循环不受影响。
        if ensure_selector_loop_on_windows():
            log.info("订阅线程已切换为 SelectorEventLoop（aiomqtt 需要）")

        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self.service.run())
        except asyncio.CancelledError:
            log.info("MQTT 订阅任务被取消（正常停止）")
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            log.exception("MQTT 订阅线程异常退出")
        finally:
            with contextlib.suppress(Exception):
                if not loop.is_closed():
                    loop.run_until_complete(self.service.flush_now())
            with contextlib.suppress(Exception):
                loop.close()
            self._loop = None
            log.info("MQTT 订阅线程已退出")

    def stop(self) -> bool:
        """请求停止并等待线程收尾。返回线程是否已退出。"""
        self.service.request_stop()          # 兜底：让 run() 的循环条件也成立

        loop, thread = self._loop, self._thread
        if loop is not None and loop.is_running():
            # 关键一步：订阅循环多半正挂在等消息上，必须从外部把它唤醒。
            # 直接 cancel 而不是等 Event —— 否则要等到下一条消息到达才退出。
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._cancel_tasks)

        if thread is not None:
            thread.join(self.stop_timeout)
            if thread.is_alive():
                log.warning("订阅线程在 %.1f 秒内未退出（daemon 线程，进程结束时会一起终止）", self.stop_timeout)
                return False
        return True

    def _cancel_tasks(self) -> None:
        """在 loop 所在线程里取消所有任务（由 call_soon_threadsafe 调度）。"""
        loop = self._loop
        if loop is None:
            return
        for task in asyncio.all_tasks(loop):
            task.cancel()

    # ---------------------------------------------------------------- 状态
    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, object]:
        uptime = (time.time() - self.started_at) if self.started_at else 0.0
        return {
            "thread_alive": self.is_alive,
            "uptime_sec": round(uptime, 1),
            "error": self.error,
            "connected": self.service.service_stats.connected,
        }
