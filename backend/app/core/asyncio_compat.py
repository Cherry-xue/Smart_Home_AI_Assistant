"""Windows 事件循环兼容处理。

问题（实测踩到）
----------------
aiomqtt 底层是 paho-mqtt 的 asyncio 封装，它用 `loop.add_reader()/add_writer()`
驱动 socket。而 Windows 默认的事件循环是 **ProactorEventLoop**，
它在 `add_writer()/remove_writer()` 上直接抛 `NotImplementedError`：

    File "asyncio\\events.py", line 564, in remove_writer
        raise NotImplementedError

**症状特别有迷惑性**：MQTT 连接建不起来（或连上后立刻断开），
但报错堆栈落在 `paho/mqtt/client.py` 的 `__del__` 里，
看起来像"垃圾回收时的无关噪声"，很容易被当成可以忽略的 warning。
实测表现就是"订阅服务一直没有 connected=True"。

解决
----
在 Windows 上改用 **SelectorEventLoop**（`WindowsSelectorEventLoopPolicy`）。

⚠️ **必须在事件循环创建之前设置** —— 也就是在任何 `asyncio.run()` 之前、
在 main.py 的最前面调用。循环已经跑起来之后改 policy 是无效的。

⚠️ **对 M2（FastAPI 集成）的影响**：uvicorn 在 Windows 上默认也用 Proactor，
所以 `app/main.py` 顶部必须调用 `ensure_selector_loop_on_windows()`，
否则订阅任务嵌进 FastAPI 后会静默失败（表现为"接口正常、数据不进库"）。
"""

from __future__ import annotations

import asyncio
import sys


def ensure_selector_loop_on_windows() -> bool:
    """在 Windows 上把事件循环策略切到 Selector。返回是否发生了切换。

    非 Windows 平台不做任何事（Linux/macOS 默认就是 Selector/uvloop）。
    """
    if sys.platform != "win32":
        return False

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:      # 理论上不会发生（3.8+ 都有）
        return False

    try:
        current = asyncio.get_event_loop_policy()
    except Exception:           # noqa: BLE001
        current = None

    if isinstance(current, policy_cls):
        return False

    asyncio.set_event_loop_policy(policy_cls())
    return True
