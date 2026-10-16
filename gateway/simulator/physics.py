"""物理引擎：按时间推演全屋状态。

三个"像真的"要点
----------------
1. **温度有惯性**：用一阶惯性 `T = T_target + (T - T_target) * exp(-dt/τ)`，
   空调制冷时 τ 小（变化快），关着时 τ 大（缓慢漂向室外温度）。
2. **功率与物理状态耦合**：空调的功率不是随便给的 —— 温度没到位就是 1100W 制冷，
   到位后降到 85W 维持。**这样能耗曲线才会出现真实的启停尖峰**，
   否则就是一条平的直线，"省电建议"也就无从谈起。
3. **人的活动有节律**：占用状态按房间、按时段、按小时做一次稳定抽样
   （同一小时内不会反复跳动），支撑"连续无活动"这类照护判断。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import random

try:  # 同时支持 `python -m gateway.simulator.publisher` 与直接当脚本运行
    from .registry import DeviceSpec
except ImportError:
    from registry import DeviceSpec

# 可控设备（下发 state）；其余按传感器处理（上报 telemetry）
ACTUATOR_TYPES = {
    "light", "curtain", "ac", "tv", "speaker", "humidifier",
    "water_heater", "door_lock", "gas_valve", "main_breaker", "camera",
}


@dataclasses.dataclass
class Sample:
    """一次采样结果。"""

    device: DeviceSpec
    kind: str                 # state / telemetry
    metrics: dict[str, object]
    units: dict[str, str]


def _parse_hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def _in_window(t: dt.time, start: str, end: str) -> bool:
    """判断时刻是否落在 [start, end) 内（支持跨零点，如 22:00-06:30）。"""
    a, b = _parse_hhmm(start), _parse_hhmm(end)
    if a <= b:
        return a <= t < b
    return t >= a or t < b


def _in_any_window(t: dt.time, windows: list[str]) -> bool:
    return any(_in_window(t, w.split("-")[0], w.split("-")[1]) for w in windows)


class HomeSimulator:
    def __init__(self, devices: list[DeviceSpec], profiles: dict, seed: int = 20261015):
        self.devices = devices
        self.p = profiles
        self.rng = random.Random(seed)

        self.room_temp: dict[str, float] = {
            room: 22.0 + self.rng.uniform(-0.8, 0.8) for room in self.p["rooms"]
        }
        self.room_humidity: dict[str, float] = {
            room: float(self.p["indoor"]["humidity_base"]) + self.rng.uniform(-4, 4)
            for room in self.p["rooms"]
        }
        self.water_temp = 42.0           # 热水器水箱温度
        self.seq = 0
        self._occ_cache: dict[tuple, bool] = {}
        self._door_open_until: dt.datetime | None = None
        self._lock_unlocked_until: dt.datetime | None = None
        self._prev_presence: dict[str, bool] = {}

        # ---- 手动覆盖（后端下发的控制指令）----
        # 结构：device_code -> {capability: value}
        #
        # 为什么要 TTL：真实设备被手动控制后，过一段时间自动策略仍会接管。
        # 如果覆盖永不过期，跑一晚上就会变成"灯永远关着"，
        # 与作息/物理模型完全脱节 —— 那样模拟器就不再是"在推演一个家"，而是"在回放一条指令"。
        self.manual_overrides: dict[str, dict[str, object]] = {}
        self.override_until: dict[str, dt.datetime] = {}
        self.by_code: dict[str, DeviceSpec] = {d.code: d for d in devices}
        #: 最近一次的模拟时间（供 MQTT 回调线程读取，见 step()）
        self.last_now: dt.datetime | None = None

        # 按房间索引可控设备，便于温度/功率耦合
        self.ac_by_room = {d.room_code: d for d in devices if d.device_type == "ac"}
        self.heater = next((d for d in devices if d.device_type == "water_heater"), None)
        self.mmwave = next((d for d in devices if d.device_type == "mmwave_radar"), None)
        self.door_sensor = next((d for d in devices if d.device_type == "door_sensor"), None)
        self.door_lock = next((d for d in devices if d.device_type == "door_lock"), None)

    # ---------------------------------------------------------------- 室外
    def outdoor_temp(self, now: dt.datetime) -> float:
        env = self.p["environment"]
        h = now.hour + now.minute / 60.0
        phase = 2 * math.pi * (h - env["outdoor_temp_peak_hour"]) / 24.0
        return env["outdoor_temp_base"] + env["outdoor_temp_amplitude"] * math.cos(phase)

    def natural_indoor(self, room: str, now: dt.datetime) -> float:
        """不开空调时，这个房间会自然稳定到的温度。

        它是"室外温度 + 室内外温差偏移"（阳台与室外几乎一致）。
        用来判断空调【有没有制冷需求】—— 见 ac_setpoint 的说明。
        """
        bias = 0.0 if room == "balcony" else self.p["environment"]["indoor_bias"]
        return self.outdoor_temp(now) + bias

    def ac_setpoint(self, now: dt.datetime) -> float:
        prof = self.p["device_types"]["ac"]["target_temp"]
        return float(prof["night"] if (now.hour >= 22 or now.hour < 7) else prof["day"])

    def ac_cooling_demand(self, dev: DeviceSpec, now: dt.datetime) -> bool:
        """这个房间【有没有】制冷需求（空调开着、且室外确实比设定值热）。

        ★ 关键物理约束：**空调只能把温度往下拉**。
        如果自然室温本来就低于设定值（比如夏夜室外只有 22℃、设定值 27℃），
        那空调即使开着也不需要制冷 —— 它不会像暖气那样把房间"加热"上去。
        """
        if not self.device_on(dev, now):
            return False
        return self.natural_indoor(dev.room_code, now) > self.ac_setpoint(now) + 0.5

    #: 恒温器死区（℃）。真实空调不是"温度高 0.01℃ 就启动"，
    #: 而是在设定值附近有一个回差：超过就制冷、降到设定值就停 ——
    #: 这样才会形成"间歇运行"的锯齿曲线（也正是能耗曲线好看的原因）。
    AC_DEADBAND = 0.3

    def ac_compressor_running(self, dev: DeviceSpec, now: dt.datetime) -> bool:
        """压缩机此刻是否在运转（不是"空调开着"，而是"正在耗大电制冷"）。"""
        if not self.ac_cooling_demand(dev, now):
            return False
        return self.room_temp[dev.room_code] > self.ac_setpoint(now) + self.AC_DEADBAND

    # ------------------------------------------------------------ 有人/无人
    def occupied(self, room: str, now: dt.datetime) -> bool:
        """按时段抽一次样，**同一小时内结果稳定**（否则灯会一秒一闪）。"""
        key = (room, now.date(), now.hour)
        if key in self._occ_cache:
            return self._occ_cache[key]
        prob = 0.0
        for w in self.p["occupancy"].get(room, []):
            if _in_window(now.time(), w["start"], w["end"]):
                prob = max(prob, float(w["prob"]))
        result = self.rng.random() < prob
        self._occ_cache[key] = result
        return result

    # -------------------------------------------------------------- 设备开关
    def _active_override(self, device_code: str, now: dt.datetime) -> dict[str, object]:
        """取该设备当前生效的手动覆盖（过期自动清理）。"""
        ov = self.manual_overrides.get(device_code)
        if not ov:
            return {}
        # ⚠️ 没有登记 `override_until` 表示**持久覆盖**（用户显式下发的指令），
        # 只有登记了过期时间的才会到点自动失效（用于临时性干预）。
        until = self.override_until.get(device_code)
        if until is not None and now >= until:
            self.manual_overrides.pop(device_code, None)
            self.override_until.pop(device_code, None)
            return {}
        return ov

    def apply_command(
        self,
        device_code: str,
        capability: str,
        value: object,
        now: dt.datetime,
        *,
        ttl_min: float | None = None,
    ) -> tuple[bool, str]:
        """应用一条来自后端的控制指令。返回 (是否接受, 说明)。

        ⚠️ 这里做的是**设备侧的自检**：就算后端已经校验过一遍，设备自己也要再验一次
        （真实设备就是这样 —— 它不会因为"上游说是合法的"就照做）。
        校验不通过**不改状态**，并且要如实回复失败原因。

        ⚠️ `ttl_min` 默认为 **None = 持久生效**。
        曾经默认 60 模拟分钟，在倍速下等于几十秒真实时间就自动失效 ——
        表现为"刚开的灯自己关了""空调设的温度自己跳回 26"。
        真实设备里你开了灯它不会自己关，所以显式指令就该一直生效，
        直到下一条指令或场景覆盖它。需要临时性干预时再显式传 `ttl_min`。
        """
        dev = self.by_code.get(device_code)
        if dev is None:
            return False, f"未知设备 {device_code}"

        cap = dev.cap(capability)
        if cap is None:
            return False, f"设备没有能力 {capability}"
        if not cap.writable:
            return False, f"{capability} 是只读能力，不接受控制指令"

        if cap.value_type == "bool":
            if not isinstance(value, bool):
                return False, f"{capability} 需要布尔值，收到 {value!r}"
        elif cap.value_type in ("int", "float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, f"{capability} 需要数值，收到 {value!r}"
            lo = float(cap.value_min) if cap.value_min is not None else None
            hi = float(cap.value_max) if cap.value_max is not None else None
            if (lo is not None and float(value) < lo) or (hi is not None and float(value) > hi):
                return False, f"{capability} 取值 {value} 超出范围 {lo}~{hi}"
        elif cap.value_type == "enum":
            if str(value) not in cap.enum_values:
                return False, f"{capability} 取值 {value!r} 不在 {cap.enum_values}"

        self.manual_overrides.setdefault(device_code, {})[capability] = value
        if ttl_min is None:
            # 持久：不登记过期时间（见 docstring 里"刚开的灯自己关了"那个坑）
            self.override_until.pop(device_code, None)
        else:
            self.override_until[device_code] = now + dt.timedelta(minutes=ttl_min)
        if ttl_min is None:
            return True, f"已应用 {capability}={value}（持续生效，直到下一条指令）"
        return True, f"已应用 {capability}={value}（{ttl_min:.0f} 模拟分钟后恢复自动推演）"

    def device_on(self, dev: DeviceSpec, now: dt.datetime) -> bool:
        # 手动覆盖优先：后端下发过开关指令就以指令为准（否则会出现
        # "灯已经关了、功率却还在按开着算"这种自相矛盾的数据）
        ov = self._active_override(dev.code, now)
        if "power_on" in ov:
            return bool(ov["power_on"])

        prof = self.p["device_types"].get(dev.device_type, {})
        windows = prof.get("on_windows") or []
        if not windows:
            return False
        if not _in_any_window(now.time(), windows):
            return False
        # 灯与影音类还要"这个房间有人"才开。
        # 空调不在此列 —— 现实中空调多是按习惯/定时开，且关了之后房间要很久才凉，
        # 用"人在才开"会让它与占用时段错开，导致能耗演示里空调几乎不干活。
        if dev.device_type in {"light", "tv", "speaker", "humidifier"}:
            return self.occupied(dev.room_code, now)
        return True

    # ------------------------------------------------------------ 温度推演
    def _step_temperature(self, now: dt.datetime, dt_min: float) -> None:
        """两步更新：先自然漂移，再按恒温器死区决定要不要制冷。

        第一步（漂移）：房间永远在向"自然温度"吸热/散热，τ 取房间值的 6 倍（慢）。
        第二步（制冷）：只有【高于设定值 + 死区】时才启动压缩机，
                       把温度快速（τ 的 0.7 倍）拉回设定值。

        这两步合起来就产生了**锯齿形的间歇运行**：
        温度慢慢爬到 26.3℃ → 压缩机启动 → 迅速回到 26.0℃ → 停机 → 再爬……
        这正是真实空调的功率曲线。
        """
        for room, cfg in self.p["rooms"].items():
            tau = float(cfg["thermal_tau_min"])
            natural = self.natural_indoor(room, now)
            cur = self.room_temp[room]

            # 第一步：向自然温度缓慢漂移
            cur = natural + (cur - natural) * math.exp(-dt_min / (tau * 6.0))

            # 第二步：需要且温度超标时，制冷把它拉回设定值
            ac = self.ac_by_room.get(room)
            if ac is not None and self.ac_cooling_demand(ac, now):
                sp = self.ac_setpoint(now)
                if cur > sp + self.AC_DEADBAND:
                    tau_cool = float(self.p["device_types"]["ac"].get("ac_cooling_tau_min", 5.0))
                    cur = sp + (cur - sp) * math.exp(-dt_min / tau_cool)

            self.room_temp[room] = cur + self.rng.gauss(0, 0.03)   # 传感器噪声

    # ------------------------------------------------------------ 湿度推演
    def _step_humidity(self, now: dt.datetime, dt_min: float) -> None:
        indoor = self.p["indoor"]
        h = now.hour + now.minute / 60.0
        for room in self.p["rooms"]:
            base = indoor["humidity_base"] + indoor["humidity_amplitude"] * math.sin(
                2 * math.pi * (h - 9) / 24.0
            )
            ac = self.ac_by_room.get(room)
            if ac and self.device_on(ac, now):
                base -= indoor["humidity_ac_drop"] * 0.5
            if room == "bathroom" and self.heater and self.device_on(self.heater, now):
                base += indoor["humidity_shower_spike"] * 0.35
            cur = self.room_humidity[room]
            self.room_humidity[room] = base + (cur - base) * math.exp(-dt_min / 18.0)
            self.room_humidity[room] += self.rng.gauss(0, 0.25)
            self.room_humidity[room] = max(15.0, min(95.0, self.room_humidity[room]))

    # ------------------------------------------------------------ 热水器水箱
    def _step_water(self, now: dt.datetime, dt_min: float) -> None:
        if not self.heater:
            return
        on = self.device_on(self.heater, now)
        if on and self.water_temp < 60.0:
            self.water_temp = 60.0 + (self.water_temp - 60.0) * math.exp(-dt_min / 12.0)
        else:
            # 不用时缓慢散热
            self.water_temp = 30.0 + (self.water_temp - 30.0) * math.exp(-dt_min / 240.0)

    # -------------------------------------------------------------- 功率
    def power_of(self, dev: DeviceSpec, now: dt.datetime) -> float:
        prof = self.p["device_types"].get(dev.device_type, {})
        on = self.device_on(dev, now)

        if dev.device_type == "ac":
            if not self.device_on(dev, now):
                return float(prof["power_off_w"])
            if self.ac_compressor_running(dev, now):
                # ★ 全功率制冷（压缩机 + 室外机）
                return float(prof["power_cooling_w"]) + self.rng.gauss(0, 18)
            # 开着但压缩机停了 → 只有室内风扇在转
            return float(prof["power_idle_w"]) + self.rng.gauss(0, 5)

        if dev.device_type == "water_heater":
            if on and self.water_temp < 59.5:
                return float(prof["power_heating_w"]) + self.rng.gauss(0, 25)
            return float(prof["power_idle_w"]) + self.rng.gauss(0, 0.3)

        base = float(prof.get("power_on_w", 0.3)) if on else float(prof.get("power_off_w", 0.3))
        return max(0.0, base + self.rng.gauss(0, base * 0.03 if base > 1 else 0.05))

    # ---------------------------------------------------------------- 采样
    def sample(self, now: dt.datetime) -> list[Sample]:
        out: list[Sample] = []
        for dev in self.devices:
            m, u = self._metrics(dev, now)
            if not m:
                continue
            kind = "state" if dev.device_type in ACTUATOR_TYPES else "telemetry"
            out.append(Sample(device=dev, kind=kind, metrics=m, units=u))
        return out

    def _metrics(self, dev: DeviceSpec, now: dt.datetime) -> tuple[dict, dict]:
        t = dev.device_type
        prof = self.p["device_types"].get(t, {})
        on = self.device_on(dev, now)
        indoor = self.p["indoor"]
        m: dict[str, object] = {}
        u: dict[str, str] = {}

        if t == "light":
            m["power_on"] = on
            if on:
                lo, hi = prof["brightness"]
                m["brightness"] = dev.clamp("brightness", self.rng.randint(lo, hi))
                u["brightness"] = "%"
        elif t == "curtain":
            pos = 100
            for win, val in prof["position_by_hour"].items():
                if _in_window(now.time(), win.split("-")[0], win.split("-")[1]):
                    pos = val
            m["position"] = dev.clamp("position", pos + self.rng.randint(-3, 3))
            u["position"] = "%"
        elif t == "ac":
            m["power_on"] = on
            m["mode"] = "cool"
            tgt = prof["target_temp"]["night" if (now.hour >= 22 or now.hour < 7) else "day"]
            m["target_temp"] = dev.clamp("target_temp", tgt)
            m["fan_speed"] = "auto"
            m["power_w"] = round(self.power_of(dev, now), 1)
            u["target_temp"] = "℃"
            u["power_w"] = "W"
        elif t == "tv":
            m["power_on"] = on
            if on:
                m["volume"] = dev.clamp("volume", self.rng.randint(*prof["volume"]))
                u["volume"] = "%"
        elif t == "speaker":
            m["power_on"] = on
            if on:
                m["volume"] = dev.clamp("volume", self.rng.randint(*prof["volume"]))
                u["volume"] = "%"
        elif t == "humidifier":
            m["power_on"] = on
            if on:
                m["target_humidity"] = dev.clamp("target_humidity", prof["target_humidity"])
                u["target_humidity"] = "%"
        elif t == "water_heater":
            m["power_on"] = on
            m["target_temp"] = dev.clamp("target_temp", prof["target_temp"])
            m["power_w"] = round(self.power_of(dev, now), 1)
            u["target_temp"] = "℃"
            u["power_w"] = "W"
        elif t == "door_lock":
            # 只在进出时段短暂解锁；其余时间保持锁定（高风险设备只上报状态，绝不执行）
            unlock = _in_window(now.time(), "08:00", "08:12") or _in_window(now.time(), "18:15", "18:27")
            m["lock_state"] = "unlocked" if unlock else "locked"
        elif t == "main_breaker":
            m["power_state"] = "on"
        elif t == "gas_valve":
            # 常态开启。⚠️ 生成器【永远不】去"关闭"它——关阀是高风险动作，
            # 只能由后端生成待确认建议，模拟器只负责上报当前阀门状态。
            m["valve_state"] = "open"
        elif t == "temp_sensor":
            m["temperature"] = round(dev.clamp("temperature", self.room_temp[dev.room_code]), 2)
            m["humidity"] = round(dev.clamp("humidity", self.room_humidity[dev.room_code]), 1)
            u["temperature"] = "℃"
            u["humidity"] = "%"
        elif t == "air_sensor":
            h = now.hour
            cooking = self.occupied("kitchen", now) and (7 <= h < 8 or 11 <= h < 13 or 17 <= h < 20)
            people = 1 if self.occupied("living_room", now) else 0
            pm = indoor["pm25_base"] + indoor["pm25_amplitude"] * math.sin(2 * math.pi * (h - 8) / 24.0)
            if cooking:
                pm += indoor["pm25_cooking_spike"]
            co2 = indoor["co2_base"] + indoor["co2_per_person"] * people
            if cooking:
                co2 += indoor["co2_cooking_spike"]
            m["pm25"] = round(dev.clamp("pm25", pm + self.rng.gauss(0, 2)), 1)
            m["co2"] = round(dev.clamp("co2", co2 + self.rng.gauss(0, 12)), 0)
            u["pm25"] = "μg/m³"
            u["co2"] = "ppm"
        elif t == "gas_sensor":
            m["gas_concentration"] = round(max(0.0, self.rng.gauss(1.5, 0.6)), 2)
            u["gas_concentration"] = "%LEL"
        elif t == "water_leak_sensor":
            m["water_detected"] = False
        elif t == "smoke_sensor":
            m["smoke_detected"] = False
        elif t == "mmwave_radar":
            present = self.occupied(dev.room_code, now)
            if present:
                if now.hour >= 21 or now.hour < 6:
                    lo, hi = self.p["activity"]["sleeping_level"]
                elif _in_window(now.time(), "02:00", "03:30"):
                    lo, hi = self.p["activity"]["moving_level"]     # 起夜
                else:
                    lo, hi = self.p["activity"]["still_level"]
            else:
                lo, hi = (0, 0)
            m["presence"] = present
            m["activity_level"] = dev.clamp("activity_level", self.rng.randint(lo, hi))
        elif t == "door_sensor":
            present = self.occupied(dev.room_code, now)
            prev = self._prev_presence.get(dev.room_code)
            # 有人状态发生跳变 → 门短暂开合
            opened = prev is not None and prev != present and self.rng.random() < 0.6
            self._prev_presence[dev.room_code] = present
            m["open"] = bool(opened)
        elif t == "camera":
            m["power_on"] = False      # 隐私优先：默认关闭
            m["recording"] = False

        # 手动覆盖优先于自动推演。
        # ⚠️ 只改"上报值"，不反过来影响温度/湿度等物理推演 ——
        # 空调被手动设成 22℃，不代表房间立刻就 22℃（那反而更不真实）。
        ov = self._active_override(dev.code, now)
        if ov:
            m.update(ov)

        return m, u

    # ------------------------------------------------------------- 推进一拍
    def step(self, now: dt.datetime, dt_min: float) -> list[Sample]:
        self.seq += 1
        # 记录"当前模拟时间"：MQTT 回调（跑在 paho 自己的线程里）需要它来给
        # 手动覆盖算有效期 —— 那边拿不到主循环的局部变量 now。
        self.last_now = now
        self._step_temperature(now, dt_min)
        self._step_humidity(now, dt_min)
        self._step_water(now, dt_min)
        return self.sample(now)
