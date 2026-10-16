"""坏数据注入。

为什么生成器要"故意产脏数据"
----------------------------
规划书要求"数据质量识别率 ≥ 95%"。如果生成的数据永远干净漂亮，
那**质量治理那一整套代码就没有任何东西可验证**，验收指标也无从谈起。

所以这里主动制造 6 类真实世界里常见的脏数据，让下游去识别、标注、剔除：

| 类型 | 模拟的现实问题 | 下游应如何反应 |
|---|---|---|
| `missing` | 丢包 / 设备离线 | 数据缺口，不得用旧值冒充 |
| `duplicate_seq` | MQTT QoS1 重投 | 按 `seq` 去重 |
| `out_of_range` | 传感器故障或标定漂移 | 标 `quality=bad` 并剔除 |
| `jump` | 瞬时干扰 / 电磁噪声 | 标 `quality=suspect`，不参与关键判断 |
| `stale` | 网关缓存后补发 | 时间戳偏旧 → 置 `is_stale` |
| `quality_bad` | 设备自检报错 | 直接标 `bad` |
"""

from __future__ import annotations

import dataclasses
import random

try:  # 同时支持两种运行方式
    from .physics import Sample
except ImportError:
    from physics import Sample


@dataclasses.dataclass
class Emission:
    """待发布的一条消息（可能带故障标记）。"""

    sample: Sample
    seq: int
    quality: str = "good"
    # 时间戳偏移（秒）。负值表示"这条数据其实是旧的"（模拟补发）
    observed_offset_sec: float = 0.0
    fault: str | None = None


@dataclasses.dataclass
class FaultStats:
    missing: int = 0
    duplicate_seq: int = 0
    out_of_range: int = 0
    jump: int = 0
    stale: int = 0
    quality_bad: int = 0
    emitted: int = 0

    def summary(self) -> str:
        return (
            f"发布 {self.emitted} 条 | 丢包 {self.missing} | 重复seq {self.duplicate_seq} | "
            f"越界 {self.out_of_range} | 跳变 {self.jump} | 过期 {self.stale} | "
            f"质量bad {self.quality_bad}"
        )


_OPS = ("missing", "duplicate_seq", "out_of_range", "jump", "stale", "quality_bad")


def _is_numeric_metric(key: str, value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and key.endswith(
        ("_w", "temp", "temperature", "humidity", "pm25", "co2", "concentration", "level", "brightness", "position", "volume")
    )


def apply(
    samples: list[Sample],
    rng: random.Random,
    rate: float,
    seq: int,
) -> tuple[list[Emission], FaultStats]:
    """按概率对采样结果注入故障。

    rate = 0 表示不加脏数据；0.05 表示约 5% 的样本会被动手脚。
    """
    stats = FaultStats()
    out: list[Emission] = []

    for s in samples:
        if rate <= 0 or rng.random() >= rate:
            out.append(Emission(sample=s, seq=seq))
            continue

        op = rng.choice(_OPS)
        # 没有数值型指标的设备做不了"越界/跳变"，降级成质量标记
        has_num = any(_is_numeric_metric(k, v) for k, v in s.metrics.items())
        if op in ("out_of_range", "jump") and not has_num:
            op = "quality_bad"

        if op == "missing":
            stats.missing += 1
            continue                            # 直接不发 —— 制造数据缺口

        if op == "duplicate_seq":
            # 连发两条、seq 相同 —— 下游必须按 seq 去重
            stats.duplicate_seq += 1
            out.append(Emission(sample=s, seq=seq, fault="duplicate_seq"))
            out.append(Emission(sample=s, seq=seq, fault="duplicate_seq"))
            continue

        if op in ("out_of_range", "jump"):
            metrics = dict(s.metrics)
            units = dict(s.units)
            keys = [k for k, v in metrics.items() if _is_numeric_metric(k, v)]
            if not keys:                        # 理论上不会走到
                out.append(Emission(sample=s, seq=seq, quality="bad", fault="quality_bad"))
                stats.quality_bad += 1
                continue
            key = rng.choice(keys)
            # 用该指标【自己】的能力范围判断越界，不要张冠李戴
            cap = s.device.cap(key)
            vmax = float(cap.value_max) if cap and cap.value_max is not None else float(metrics[key]) * 3 + 10
            vmin = float(cap.value_min) if cap and cap.value_min is not None else 0.0
            if op == "out_of_range":
                metrics[key] = round(vmax * 1.5 + 5, 2)     # 明确超出能力档案上限
                quality, stats.out_of_range = "bad", stats.out_of_range + 1
            else:
                # 跳变：瞬间跳到量程边缘（幅度不合物理）
                metrics[key] = round(vmax if rng.random() < 0.5 else vmin, 2)
                quality, stats.jump = "suspect", stats.jump + 1
            out.append(
                Emission(
                    sample=Sample(s.device, s.kind, metrics, units),
                    seq=seq,
                    quality=quality,
                    fault=op,
                )
            )
            continue

        if op == "stale":
            stats.stale += 1
            out.append(Emission(sample=s, seq=seq, observed_offset_sec=-rng.uniform(300, 1800), fault="stale"))
            continue

        # quality_bad
        stats.quality_bad += 1
        out.append(Emission(sample=s, seq=seq, quality="bad", fault="quality_bad"))

    stats.emitted = len(out)
    return out, stats
