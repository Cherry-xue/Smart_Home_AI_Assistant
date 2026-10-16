"""数据质量判定：去重、时间倒退（补发/乱序）、越界标注。

为什么这条链路必须有质量层
--------------------------
生成器会**故意注入 6 类坏数据**（丢包 / 重复 seq / 越界 / 跳变 / 过期 / 质量坏），
这是为了让"数据质量治理"有东西可验证。如果订阅端"收到就写库"，
这六类脏数据就会和真实读数混在一起，后面所有趋势分析、异常判断、
能耗统计都会被污染 —— 而且**污染是静默的**，你不会收到任何错误。

处理原则：**不丢原文，但必须打标**
--------------------------------
| 情况 | 处理 |
|---|---|
| 完全重复的消息（同设备 + 同 seq 重发） | **丢弃**，只在统计里计数 |
| 时间戳倒退（网关补发 / 乱序到达） | 入库，`is_stale = 1` |
| 值超出能力档案范围 | 入库，`quality` 降级为 `suspect` |
| 发布方直接标了 bad / suspect | 原样透传 |

下游只要写 `WHERE quality = 'good' AND is_stale = 0` 就能拿到干净数据。

⚠️ 这里**不用 wall clock 判断过期**。模拟器可以 `--speed 60` 压缩时间、
也可以 `--start` 从过去某个时刻开始跑，用"现在几点"去比会大面积误判。
改用**同设备上一条的 observed_at** 做相对比较 —— 只关心"时间有没有倒退"，
与当前真实时间无关，也就与时区/倍速设置无关。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import deque

from app.db.catalog import DeviceCatalog, DeviceInfo
from app.mqtt.envelope import Envelope

#: 允许的时间倒退容忍度（秒）。同一 tick 内的抖动不算倒退，
#: 只有明确往回走很多（生成器注入的补发是 -300 ~ -1800 秒）才判 stale。
STALE_BACKWARD_TOLERANCE_SEC = 60.0

#: 每个设备记住多少个最近的 seq 用于去重。
#: 取 64 是因为"同 tick 重复发送"是紧邻的，不需要记太久。
SEQ_MEMORY = 64

QUALITY_ORDER = {"good": 0, "suspect": 1, "bad": 2}


@dataclasses.dataclass
class Verdict:
    """一条消息的质量判定结果。"""

    accepted: bool
    quality: str
    is_stale: bool
    notes: list[str] = dataclasses.field(default_factory=list)
    out_of_range: list[str] = dataclasses.field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.quality != "good" or self.is_stale


class QualityEvaluator:
    """有状态的质检器（需要记住"上一条"才能判断重复与倒退）。

    只在**接受**一条消息后才更新记忆 —— 被丢弃的重复消息不能影响状态，
    否则重复消息会把 last_seen 搅乱，导致后续正常消息被误判为倒退。
    """

    def __init__(
        self,
        catalog: DeviceCatalog,
        *,
        stale_backward_sec: float = STALE_BACKWARD_TOLERANCE_SEC,
        seq_memory: int = SEQ_MEMORY,
    ):
        self.catalog = catalog
        self.stale_backward_sec = stale_backward_sec
        self.seq_memory = seq_memory
        self._recent_seq: dict[str, deque[int]] = {}
        self._last_seen: dict[str, dt.datetime] = {}
        self.duplicates = 0
        self.stale_count = 0
        self.out_of_range_count = 0

    def evaluate(
        self,
        env: Envelope,
        observed_local: dt.datetime,
        device: DeviceInfo,
        *,
        retain: bool = False,
    ) -> Verdict:
        notes: list[str] = []

        # ---------- 1) 去重 ----------
        # 注意 seq 是【全局 tick 序号】，同一 tick 里 32 台设备共享同一个 seq，
        # 所以去重键必须是 (device_code, seq)，不能只看 seq。
        if env.seq is not None:
            seen = self._recent_seq.setdefault(env.device_code, deque(maxlen=self.seq_memory))
            if env.seq in seen:
                self.duplicates += 1
                return Verdict(False, env.quality, False, ["重复 seq（QoS1 重投）"])
            seen.append(env.seq)

        # ---------- 2) 时间倒退 → 补发/乱序 ----------
        # ⚠️ retained 快照不参与这个判断，也不更新基准。
        # retained 是"订阅瞬间 broker 回放的最后一帧状态"，属于会话初始化快照，
        # 不是实时流的一部分。拿它的时间戳当基准，之后所有实时数据都会被判成倒退
        # —— 实测踩过：broker 回放 22 条上一轮的 state（时间戳比新一轮晚 2 小时），
        # 导致新一轮 510 条数据全部被误标 is_stale，连最新值缓存都跟着失效。
        last = None if retain else self._last_seen.get(env.device_code)
        is_stale = False
        if last is not None and (last - observed_local).total_seconds() > self.stale_backward_sec:
            is_stale = True
            self.stale_count += 1
            notes.append(f"时间戳倒退 {int((last - observed_local).total_seconds())}s（补发/乱序）")
        elif retain:
            notes.append("retained 快照（会话初始化回放，不参与时间基准）")

        # ---------- 3) 越界（值超出能力档案声明的范围）----------
        out_of_range: list[str] = []
        for metric, value in env.metrics.items():
            cap = device.cap(metric)
            if cap is not None and not cap.contains(value):
                out_of_range.append(metric)
        if out_of_range:
            self.out_of_range_count += 1
            notes.append("越界: " + ", ".join(out_of_range))

        # ---------- 4) 合成质量标记 ----------
        quality = env.quality
        if out_of_range and QUALITY_ORDER.get(quality, 1) < QUALITY_ORDER["suspect"]:
            quality = "suspect"          # 发布方没发现，我们替它降级

        # ---------- 5) 更新记忆（只对接受的、非 retained 的消息）----------
        if not retain and (last is None or observed_local > last):
            self._last_seen[env.device_code] = observed_local

        return Verdict(True, quality, is_stale, notes, out_of_range)
