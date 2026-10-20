"""进程内 TTL 缓存：给"算起来贵、又不需要秒级新鲜"的只读汇总接口用。

为什么需要
----------
`/api/energy/summary` 要在 24 小时窗口上做三次聚合（分时段 / 逐小时 / 分设备），
`energy_readings` 几十万行，实测稳定 **1.1 秒**。而能耗是慢变量 ——
20 秒前的账单数字和现在没有区别，没必要每个页面都重算一遍。

只给**只读服务**用：写操作、指令、场景执行都不经过这里，
所以不存在"改了数据却读到旧值"的问题。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable


class TTLCache:
    """极简 TTL 缓存（线程安全）。

    刻意不引入 cachetools / redis：单机、单进程、几个 key，
    几十行代码足够，多一个依赖就多一份要解释的东西。
    """

    def __init__(self, ttl: float = 20.0, maxsize: int = 128) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            at, value = hit
            if time.monotonic() - at >= self._ttl:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self._maxsize:
                # 淘汰最旧的一半：key 数量本来就少，不需要 LRU 那套
                oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])[: self._maxsize // 2]
                for k, _ in oldest:
                    self._data.pop(k, None)
            self._data[key] = (time.monotonic(), value)

    def get_or(self, key: Hashable, factory: Callable[[], Any]) -> Any:
        """命中就返回，未命中才算。**计算过程不持锁**，避免慢查询挡住别的 key。"""
        hit = self.get(key)
        if hit is not None:
            return hit
        value = factory()
        self.set(key, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"entries": len(self._data), "ttl_sec": self._ttl}
