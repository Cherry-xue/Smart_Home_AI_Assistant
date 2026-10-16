"""分时电价：把"某个时刻属于峰/平/谷、单价多少"做成确定性代码。

为什么要独立成一个模块
----------------------
电价有两个消费者：① 落库时给 `energy_readings.tariff_period` 打标签；
② 能耗 Agent 算"把加热挪到谷电时段能省多少钱"。

规划书有一条红线：**数据不足时不给确定性省费金额，且金额必须由确定性代码算，
不让模型心算**。所以电价与算钱的逻辑必须在一个可测试、可复用的地方，
而不是塞进提示词里让模型"估一估"。

配置读 `config/tariff.yaml`（项目级共享文件，生成器也读同一份）。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
from pathlib import Path

import yaml

# backend/app/core/tariff.py -> 上溯 3 层为项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]
TARIFF_PATH = PROJECT_ROOT / "config" / "tariff.yaml"

VALID_PERIODS = ("peak", "flat", "valley")


def _parse_hhmm(s: str) -> dt.time:
    h, m = str(s).split(":")
    return dt.time(int(h), int(m))


@dataclasses.dataclass(frozen=True)
class TariffBand:
    period: str
    start: dt.time
    end: dt.time
    price: float

    def contains(self, t: dt.time) -> bool:
        """时段是否覆盖该时刻。支持跨零点（如 21:00-08:00）。"""
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


class Tariff:
    """峰平谷电价表。"""

    def __init__(self, bands: list[TariffBand], currency: str = "CNY", unit: str = "kWh"):
        self.bands = bands
        self.currency = currency
        self.unit = unit

    @classmethod
    def load(cls, path: Path | str = TARIFF_PATH) -> Tariff:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        bands: list[TariffBand] = []
        for b in raw.get("bands", []):
            period = str(b["period"]).strip().lower()
            if period not in VALID_PERIODS:
                raise ValueError(f"未知电价时段 {period!r}，可选 {VALID_PERIODS}")
            bands.append(
                TariffBand(
                    period=period,
                    start=_parse_hhmm(b["start"]),
                    end=_parse_hhmm(b["end"]),
                    price=float(b["price"]),
                )
            )
        return cls(bands, raw.get("currency", "CNY"), raw.get("unit", "kWh"))

    def band_at(self, moment: dt.datetime | dt.time) -> TariffBand | None:
        t = moment.time() if isinstance(moment, dt.datetime) else moment
        for band in self.bands:
            if band.contains(t):
                return band
        return None

    def period_at(self, moment: dt.datetime | dt.time) -> str | None:
        """返回 peak / flat / valley；不在任何时段内返回 None（宁可为空也不猜）。"""
        band = self.band_at(moment)
        return band.period if band else None

    def price_at(self, moment: dt.datetime | dt.time) -> float | None:
        band = self.band_at(moment)
        return band.price if band else None

    def describe(self) -> str:
        return " · ".join(
            f"{b.period}({b.start:%H:%M}-{b.end:%H:%M} ¥{b.price:.2f})" for b in self.bands
        )


@functools.lru_cache(maxsize=1)
def get_tariff() -> Tariff:
    return Tariff.load()
