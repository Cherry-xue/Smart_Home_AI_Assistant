"""设备注册表：从数据库读取设备清单与能力档案。

为什么从数据库读而不是在 YAML 里再写一份设备列表
--------------------------------------------------
设备清单已经有唯一来源（`devices` + `device_capabilities` 表）。
再复制一份到 YAML，就会出现"库里删了设备、生成器还在产它的数据"这种漂移。

顺带的好处：生成器能直接拿到能力档案声明的取值范围，
**产出的数值天然不会超出设备能力**（不会出现"亮度 150%"这种自相矛盾的数据）。

本模块是独立的（自己读 .env、自己用 pymysql），不 import backend，
因为生成器是按"独立进程"设计的（选型里 paho-mqtt 只给它用）。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib

import pymysql
import yaml

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
PROFILES_PATH = pathlib.Path(__file__).parent / "device_profiles.yaml"


def load_env() -> dict[str, str]:
    """从项目根 .env 读环境变量（不依赖 python-dotenv，保持零外部依赖）。"""
    env: dict[str, str] = {}
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        raise FileNotFoundError(f"找不到 .env：{path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def load_profiles() -> dict:
    with PROFILES_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def connect(env: dict[str, str]):
    """按 .env 里的凭据建一个连接（生成器是独立进程，不复用后端引擎）。"""
    return pymysql.connect(
        host=env["MYSQL_HOST"],
        port=int(env.get("MYSQL_PORT", 3306)),
        user=env["MYSQL_USER"],
        password=env["MYSQL_PASSWORD"],
        database=env["MYSQL_DB"],
        charset="utf8mb4",
        connect_timeout=8,
        cursorclass=pymysql.cursors.DictCursor,
    )


def latest_observed_at(env: dict[str, str]) -> dt.datetime | None:
    """库里最新一条遥测的时间（naive 本地时间）。

    用途：`--start auto` 的续跑判断。

    ⚠️ 为什么需要它：多轮运行时如果每轮都从"当前时刻"重新开始，而库里
    （以及 broker 上 retained 的 state）已经存在【更晚时间戳】的数据，
    **整个数据源的时间轴就倒退了**。订阅端有一层"时间倒退 → is_stale"的
    数据质检，会把整轮新数据都判成"补发数据"（实测踩到：一轮 510 条被误标）。
    """
    conn = connect(env)
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(observed_at) AS latest FROM device_readings")
        row = cur.fetchone()
        return row["latest"] if row and row["latest"] else None
    finally:
        conn.close()


def load_last_commands(env: dict[str, str]) -> dict[tuple[str, str], object]:
    """取每台设备每个能力**最近一条已执行**的指令，用于启动时恢复设备状态。

    ⚠️ 为什么需要它：模拟器的状态在内存里，进程一重启就回到 profile 默认值 ——
    表现为"重启一次，之前开的灯全灭了、空调设定的温度跳回 26"。
    真实设备断电重启后开关状态是保持的，所以这里在启动时把数据库里
    `status='executed'` 的最后状态重放一遍。

    ⚠️ 只认 `executed`：高风险指令（门锁/燃气阀/总闸）在库里恒为
    `pending_confirm`，**不会被重放** —— 那条红线不能因为重启就被绕过。
    """
    conn = connect(env)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT d.code AS code, c.command AS capability, c.params_json AS params
            FROM device_commands c
            JOIN devices d ON d.id = c.device_id
            WHERE c.status = 'executed'
            ORDER BY c.id
            """
        )
        out: dict[tuple[str, str], object] = {}
        for row in cur.fetchall():
            try:
                params = json.loads(row["params"] or "{}")
            except Exception:  # noqa: BLE001
                continue
            if "value" in params:
                out[(row["code"], row["capability"])] = params["value"]
        return out
    finally:
        conn.close()


@dataclasses.dataclass(frozen=True)
class Capability:
    code: str
    display_name: str
    value_type: str
    unit: str | None
    value_min: float | None
    value_max: float | None
    step: float | None
    enum_values: list[str]
    writable: bool
    risk_level: str


@dataclasses.dataclass(frozen=True)
class DeviceSpec:
    id: int
    code: str
    name: str
    device_type: str
    room_code: str
    room_name: str
    home_id: int
    high_risk: bool
    capabilities: list[Capability]

    def cap(self, code: str) -> Capability | None:
        for c in self.capabilities:
            if c.code == code:
                return c
        return None

    def clamp(self, code: str, value: float) -> float:
        """把取值夹到能力档案声明的范围内 —— 生成器不产超出能力的数据。"""
        c = self.cap(code)
        if c is None:
            return value
        lo = c.value_min if c.value_min is not None else value
        hi = c.value_max if c.value_max is not None else value
        return max(float(lo), min(float(hi), float(value)))


def load_devices(env: dict[str, str]) -> list[DeviceSpec]:
    conn = connect(env)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT d.id, d.code, d.name, d.device_type, d.high_risk, d.home_id,
                   r.code AS room_code, r.name AS room_name
            FROM devices d
            JOIN rooms r ON r.id = d.room_id
            WHERE d.enabled = 1
            ORDER BY r.sort_no, d.id
            """
        )
        rows = cur.fetchall()

        cur.execute(
            """
            SELECT dc.device_id, dc.capability_code, dc.display_name, dc.value_type,
                   dc.unit, dc.value_min, dc.value_max, dc.step, dc.enum_values,
                   dc.writable, dc.risk_level
            FROM device_capabilities dc
            JOIN devices d ON d.id = dc.device_id
            WHERE d.enabled = 1
            ORDER BY dc.device_id, dc.id
            """
        )
        cap_rows = cur.fetchall()
    finally:
        conn.close()

    caps_by_device: dict[int, list[Capability]] = {}
    for r in cap_rows:
        caps_by_device.setdefault(r["device_id"], []).append(
            Capability(
                code=r["capability_code"],
                display_name=r["display_name"],
                value_type=r["value_type"],
                unit=r["unit"],
                value_min=float(r["value_min"]) if r["value_min"] is not None else None,
                value_max=float(r["value_max"]) if r["value_max"] is not None else None,
                step=float(r["step"]) if r["step"] is not None else None,
                enum_values=[x for x in (r["enum_values"] or "").split(",") if x],
                writable=bool(r["writable"]),
                risk_level=r["risk_level"],
            )
        )

    return [
        DeviceSpec(
            id=r["id"],
            code=r["code"],
            name=r["name"],
            device_type=r["device_type"],
            room_code=r["room_code"],
            room_name=r["room_name"],
            home_id=r["home_id"],
            high_risk=bool(r["high_risk"]),
            capabilities=caps_by_device.get(r["id"], []),
        )
        for r in rows
    ]
