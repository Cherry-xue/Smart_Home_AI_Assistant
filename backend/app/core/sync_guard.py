"""`sync_*` 推导的节流器。

为什么需要
----------
告警 / 照护 / 建议都是**读时推导**：每次列表请求都从 24 小时窗口全量重算一遍。
推算本身是幂等的，但代价不低。实测（2026-10-20，1 号房）：

| 接口 | 总计 | 其中 sync | 纯查询 |
|---|---|---|---|
| `/api/care/observations` | 352 ms | **347 ms** | 5 ms |
| `/api/suggestions` | 569 ms | **562 ms** | 7 ms |
| `/api/security/alarms` | 18 ms | 11 ms | 7 ms |

也就是说切到"照护与安防"页时那几百毫秒，几乎全是重复推导 —— 而推导结果
在几秒内不会变。所以给每个 `(种类, 套房)` 一个最小间隔：间隔内重复请求
**直接跳过推导**，用库里已有的结果（幂等性保证跳过是安全的）。

间隔取 8 秒：前端 pending/health 轮询是 6~10 秒，正好一次轮询最多触发一次推导。

⚠️ 这是"少算几次"，不是"缓存响应"。响应仍然每次现查库，所以用户
`已知悉` 一条告警后，列表状态立刻是新的，不存在读到旧响应的问题。
"""

from __future__ import annotations

import threading
import time

#: 每种推导的最小间隔（秒）。想更实时就调小，想更快就调大。
DEFAULT_MIN_INTERVAL = 8.0

_last_run: dict[str, float] = {}
_lock = threading.Lock()


def should_run(kind: str, key: object, min_interval: float = DEFAULT_MIN_INTERVAL) -> bool:
    """这次请求要不要跑推导？

    `kind` 是推导种类（alarms / care / suggestions），`key` 通常是套房 id
    或数据锚点 —— 同一个 key 在 `min_interval` 内只跑一次。
    """
    token = f"{kind}:{key}"
    now = time.monotonic()
    with _lock:
        last = _last_run.get(token)
        if last is not None and now - last < min_interval:
            return False
        _last_run[token] = now
        return True


def touch(kind: str, key: object) -> None:
    """手动把某个 key 标记为"刚跑过"。

    用于"人工动作之后顺便推导一次"的场景：比如 `ack` 一条告警之后，
    紧接着的列表请求不必再推一遍。
    """
    with _lock:
        _last_run[f"{kind}:{key}"] = time.monotonic()


def reset() -> None:
    """清空记录（测试用）。"""
    with _lock:
        _last_run.clear()
