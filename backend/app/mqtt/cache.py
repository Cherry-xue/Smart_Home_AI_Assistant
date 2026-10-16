"""最新值缓存：进程内保存"每台设备每个指标的最后一次可信读数"。

三个用途
--------
1. **快**：API / Agent 问"客厅现在多少度"时直接命中内存，不用打远程库
   （这是最高频的问题，远程库一次往返是几十毫秒）。
2. **设备离线判定**：记着每台设备的最后上报时间，就能报出"哪台设备多久没动静了"
   —— 传感器掉线往往比它报错更值得提醒。
3. **重启后立刻可用**：`preload()` 从库里把最近的可信读数灌进来，
   服务刚起来就能回答"现在多少度"，而不是等 MQTT 来第一条消息。

⚠️ 线程安全（重要）
-------------------
MQTT 订阅服务跑在**独立线程**里（自带事件循环，见 `mqtt/service_worker.py`），
FastAPI 的端点跑在 uvicorn 的主循环线程里 —— 两边**共享这一份缓存**。
所以所有读写都加锁：CPython 的单次 dict 操作虽然原子，但
`snapshot()` 这种"边遍历边被改"的操作会直接抛
`RuntimeError: dictionary changed size during iteration`。

用 `RLock` 而不是 `Lock`：`update()` 内部会调用只读方法，可重入更省心。

⚠️ **只收 quality = good 且非 stale 的值**。
"最新值"一旦被脏数据污染，后面所有基于它的判断都会跟着错。
坏数据仍然会入库留痕（审计与质量分析需要），只是不进缓存。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import threading
from decimal import Decimal
from typing import Any

from app.db.catalog import CapabilityRange


@dataclasses.dataclass(frozen=True)
class LatestValue:
    value: object
    observed_at: dt.datetime
    unit: str | None = None
    quality: str = "good"
    seq: int | None = None


def coerce_from_db(
    value_num: Decimal | float | None,
    value_text: str | None,
    cap: CapabilityRange | None,
) -> object:
    """把数据库里的 (value_num, value_text) 还原成带类型的 Python 值。

    为什么需要它：入库时我们把 bool 编成了 1.0/0.0（便于聚合），
    读回来时如果不看能力档案就会变成 `1.0` 而不是 `true` ——
    API 返回给前端就不好看了。**能力档案的 value_type 是还原类型的依据**，
    这也符合"能力档案是设备功能唯一事实来源"这条设计原则。
    """
    vtype = cap.value_type if cap is not None else None
    if value_text is not None and value_num is None:
        return value_text
    if value_num is None:
        return value_text
    if vtype == "bool":
        return bool(value_num)
    if vtype == "int":
        return int(value_num)
    # float / 未知类型：保留数值。用 float 而不是 Decimal，
    # 这样和实时消息里的 JSON 数字表示一致（同一份缓存不该有两种数字形态）。
    return float(value_num)


class LatestValueCache:
    def __init__(self) -> None:
        # device_code -> {metric_code: LatestValue}
        self._values: dict[str, dict[str, LatestValue]] = {}
        # device_code -> 最后一次【实时上报】时间
        #
        # ⚠️ 只由 update()（实时消息）更新，preload() 不会碰它。
        # 为什么必须分开：`preload` 灌进来的是"库里最后一条读数"，
        # 它只能说明"值是多少"，**不能说明"数据源现在活着"**。
        # 混在一起会出现很荒谬的结论 —— 实测踩过：MQTT 完全没连上，
        # 但库里最新一条数据的时间戳很新，于是 32 台设备全被标成"在线"。
        self._last_seen_live: dict[str, dt.datetime] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 写入
    def update(
        self,
        device_code: str,
        metrics: dict[str, object],
        observed_at: dt.datetime,
        *,
        units: dict[str, str] | None = None,
        quality: str = "good",
        is_stale: bool = False,
        seq: int | None = None,
        received_at: dt.datetime | None = None,
        count_as_live: bool = True,
    ) -> int:
        """写入一次读数。返回实际写入的指标个数（脏数据返回 0）。

        两个时间参数语义**完全不同**，别混用：

        | 参数 | 含义 | 用途 |
        |---|---|---|
        | `observed_at` | 设备/网关报出的**观测时间**。可能不准、可能在未来、可能是补发 | 展示"这个值是什么时候测的" |
        | `received_at` | 我们**实际收到消息的本机时间**（默认取 now） | **判断设备是否在线** |

        ⚠️ 在线判定必须用 `received_at`。观测时间来自设备时钟，拿它做在线判定
        会得出"设备时钟跑到未来 = 永远在线"这种荒谬结论（实测踩到过）。

        `count_as_live=False`：这条消息不算"在线"证据 —— 用于 broker 回放的
        retained 快照：它只说明"broker 上存着它的最后状态"，不说明"它现在在发数据"。
        """
        if quality != "good" or is_stale:
            return 0

        units = units or {}
        seen_at = received_at or dt.datetime.now()
        with self._lock:
            bucket = self._values.setdefault(device_code, {})
            for metric, value in metrics.items():
                bucket[metric] = LatestValue(
                    value=value,
                    observed_at=observed_at,
                    unit=units.get(metric),
                    quality=quality,
                    seq=seq,
                )
            if count_as_live:
                prev = self._last_seen_live.get(device_code)
                if prev is None or seen_at > prev:
                    self._last_seen_live[device_code] = seen_at
        return len(metrics)

    def preload(
        self,
        device_code: str,
        metric_values: dict[str, tuple[object, dt.datetime]],
        units: dict[str, str | None] | None = None,
    ) -> int:
        """批量灌入历史最新值（供启动时预热）。

        只补空缺：**已经有实时值就不覆盖** —— 库里的是"上次进程退出前"的状态，
        实时消息才代表此刻。

        ⚠️ 刻意**不更新** `_last_seen_live`：预热的数据不能证明"数据源现在活着"，
        否则"设备在线状态"会立刻失真（见 `_last_seen_live` 的说明）。
        """
        units = units or {}
        filled = 0
        with self._lock:
            bucket = self._values.setdefault(device_code, {})
            for metric, (value, observed_at) in metric_values.items():
                existing = bucket.get(metric)
                if existing is not None and existing.observed_at >= observed_at:
                    continue
                bucket[metric] = LatestValue(
                    value=value, observed_at=observed_at, unit=units.get(metric)
                )
                filled += 1
        return filled

    # ---------------------------------------------------------------- 读取
    def get(self, device_code: str, metric: str) -> LatestValue | None:
        with self._lock:
            return self._values.get(device_code, {}).get(metric)

    def value_of(self, device_code: str, metric: str, default: object = None) -> object:
        lv = self.get(device_code, metric)
        return lv.value if lv is not None else default

    def device(self, device_code: str) -> dict[str, LatestValue]:
        with self._lock:
            return dict(self._values.get(device_code, {}))

    def last_seen(self, device_code: str) -> dt.datetime | None:
        """最后一次**实时上报**时间（用于"在线"判定）。

        预热进来的历史值不会影响它 —— 详见 `_last_seen_live` 的说明。
        """
        with self._lock:
            return self._last_seen_live.get(device_code)

    def observed_extent(self, device_code: str) -> dt.datetime | None:
        """这台设备所有已知读数里**最新的观测时间**（含预热数据）。

        与 `last_seen()` 的区别：这个回答"我们手上最新的数据是什么时候的"，
        而 `last_seen()` 回答"数据源最后一次说话是什么时候"。
        前端展示"数据更新时间"要用它（否则重启后明明有数据却显示"从无数据"）。
        """
        with self._lock:
            bucket = self._values.get(device_code)
            if not bucket:
                return None
            return max(lv.observed_at for lv in bucket.values())

    def devices_without_update(
        self, now: dt.datetime, max_age_sec: float, known_devices: list[str] | None = None
    ) -> list[str]:
        """返回超过 max_age_sec 没有**实时上报**的设备（含从没上报过的）。

        `now` 由调用方传入而不是在这里取 —— 便于测试，也避免隐式依赖当前时间。
        """
        with self._lock:
            candidates = (
                known_devices if known_devices is not None else list(self._last_seen_live)
            )
            stale: list[str] = []
            for code in candidates:
                last = self._last_seen_live.get(code)
                if last is None or (now - last).total_seconds() > max_age_sec:
                    stale.append(code)
            return stale

    def expected_interval_hint(self) -> float:
        """给"多久算离线"用的粗粒度建议值（秒）。

        取所有设备最近上报间隔的中位数会让实现变重；这里用一个够用的固定值：
        设备是秒级上报的，几分钟没动静基本就是有问题了。
        """
        return 300.0

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """扁平快照：{device_code: {metric: value}}（给 API 用）。"""
        with self._lock:
            return {
                dev: {metric: lv.value for metric, lv in bucket.items()}
                for dev, bucket in self._values.items()
            }

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "devices": len(self._values),
                "values": sum(len(b) for b in self._values.values()),
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)
