"""预设设备与设备的增删（软删）。

用户在界面上「添加设备」时**只能从 `config/device_presets.yaml` 里选**，不能自定义。

三条设计红线
------------
1. ⚠️⚠️ **`high_risk` 由 `device_type` 推导，绝不接受外部声明。**
   如果让它可配置，用户加一个 `door_lock` 类型、把 high_risk 填 0，
   就把"门锁/燃气阀/总闸永不自动执行"这条红线绕过去了 ——
   而前提所有强制改写（`executor.create_command`）和待确认项，
   全都建立在这个字段可信的基础上。
   `load_presets()` 里有一道断言：预设文件里出现 `high_risk` 就直接报错，
   性质与 `tools/__init__._assert_no_execution_channel()` 相同 ——
   让半年后有人想加这一项时当场看见理由。

2. **删除是软删（`devices.enabled = 0`），不是 DELETE。**
   后端目录和生成器的 SQL 都已经写了 `WHERE d.enabled = 1`，所以软删是零成本的；
   而硬删会被外键挡住（`device_commands` / `sensor_events` 都是 ON DELETE NO ACTION，
   报 1451），并且会在 `device_readings` 留下孤儿行（那张表**没有**指向 devices 的外键）。

3. **不改能力档案的形状。** 预设里的取值类型/范围照抄数据库现有档案，
   所以新设备与原有设备完全同构，生成器/前端/校验链都不用改。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config.settings import PROJECT_ROOT
from app.models.orm import Device, DeviceCapability

log = logging.getLogger("app.services.device_admin")

PRESETS_PATH = PROJECT_ROOT / "config" / "device_presets.yaml"

#: 数据库 `device_capabilities.value_type` 的合法取值（enum 列）
VALID_VALUE_TYPES = {"bool", "int", "float", "enum", "string"}

#: ⚠️ **高风险设备类型白名单** —— 唯一事实来源。
#: 门锁 / 燃气阀 / 电气总闸：任何入口都只能生成待确认项，永不自动执行。
HIGH_RISK_TYPES = frozenset({"door_lock", "gas_valve", "main_breaker", "breaker"})


class DeviceAdminError(Exception):
    """校验不通过。带 HTTP 状态码，交给 API 层直接抛。"""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def derive_high_risk(device_type: str) -> bool:
    """设备的"高风险"只由类型决定 —— 不看请求、不看预设文件。"""
    return str(device_type or "").strip().lower() in HIGH_RISK_TYPES


# ============================================================ 预设
@dataclasses.dataclass(frozen=True)
class Preset:
    key: str
    label: str
    device_type: str
    note: str
    capabilities: tuple[dict[str, Any], ...]

    @property
    def high_risk(self) -> bool:
        return derive_high_risk(self.device_type)

    @property
    def writable_count(self) -> int:
        return sum(1 for c in self.capabilities if c.get("writable"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "device_type": self.device_type,
            "note": self.note,
            "high_risk": self.high_risk,
            "capability_count": len(self.capabilities),
            "writable_count": self.writable_count,
            "capabilities": [dict(c) for c in self.capabilities],
        }


_presets_cache: list[Preset] | None = None


def load_presets(path: Path | None = None, *, force: bool = False) -> list[Preset]:
    """读预设清单（带缓存）。文件内容非法就抛错，**不做静默兜底**。"""
    global _presets_cache
    if _presets_cache is not None and path is None and not force:
        return _presets_cache

    p = Path(path) if path else PRESETS_PATH
    if not p.exists():
        raise DeviceAdminError(f"找不到预设设备清单：{p}", status_code=500)

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    items = raw.get("presets") or []

    out: list[Preset] = []
    seen: set[str] = set()
    for i, entry in enumerate(items):
        if not isinstance(entry, dict):
            raise DeviceAdminError(f"预设第 {i + 1} 条不是映射：{entry!r}", status_code=500)

        # ⚠️ 红线断言：预设文件里不许出现 high_risk
        if "high_risk" in entry:
            raise DeviceAdminError(
                f"预设 {entry.get('key')!r} 声明了 high_risk —— 这是被禁止的。"
                "设备的危险等级必须由 device_type 推导（HIGH_RISK_TYPES），"
                "否则有人把门锁的 high_risk 填成 0 就能绕过高风险确认。",
                status_code=500,
            )

        key = str(entry.get("key") or "").strip()
        dtype = str(entry.get("device_type") or "").strip()
        if not key or not dtype:
            raise DeviceAdminError(f"预设第 {i + 1} 条缺 key 或 device_type", status_code=500)
        if key in seen:
            raise DeviceAdminError(f"预设 key 重复：{key}", status_code=500)
        seen.add(key)

        caps: list[dict[str, Any]] = []
        for c in entry.get("capabilities") or []:
            code = str(c.get("code") or "").strip()
            vt = str(c.get("value_type") or "").strip()
            if not code:
                raise DeviceAdminError(f"预设 {key} 里有能力缺 code", status_code=500)
            if vt not in VALID_VALUE_TYPES:
                raise DeviceAdminError(
                    f"预设 {key} 的能力 {code} 的 value_type={vt!r} 非法，"
                    f"只允许 {sorted(VALID_VALUE_TYPES)}",
                    status_code=500,
                )
            caps.append(
                {
                    "code": code,
                    "display_name": str(c.get("display_name") or code),
                    "value_type": vt,
                    "unit": c.get("unit"),
                    "value_min": c.get("value_min"),
                    "value_max": c.get("value_max"),
                    "step": c.get("step"),
                    "enum_values": c.get("enum_values") or "",
                    "writable": bool(c.get("writable")),
                    "risk_level": str(c.get("risk_level") or "low"),
                }
            )
        if not caps:
            raise DeviceAdminError(f"预设 {key} 没有任何能力", status_code=500)

        out.append(
            Preset(
                key=key,
                label=str(entry.get("label") or key),
                device_type=dtype,
                note=str(entry.get("note") or ""),
                capabilities=tuple(caps),
            )
        )

    if path is None:
        _presets_cache = out
    log.info("预设设备清单已加载：%d 条（其中高风险 %d 条）",
             len(out), sum(1 for x in out if x.high_risk))
    return out


def get_preset(key: str) -> Preset | None:
    for p in load_presets():
        if p.key == key:
            return p
    return None


# ============================================================ 生成编码
def _next_device_code(db: Session, device_type: str) -> str:
    """给新设备编一个唯一的 code。

    code 会进 MQTT topic，所以必须是**ASCII 小写无空格**；这里用
    `<device_type>_<序号>`（例如 `light_10`），从 1 开始找第一个没用过的。

    ⚠️ **全库唯一，不是"每套房唯一"**：`DeviceCatalog` 用 code 做 key，
       两套房各有一个 `temp_sensor_1` 时，目录里后加载的那台会覆盖前一台 ——
       表现是"某套房莫名少了一台设备"，读数还会串到另一个家。
    """
    base = "".join(ch for ch in device_type.lower() if ch.isalnum() or ch == "_") or "device"
    used = set(db.execute(select(Device.code).where(Device.code.like(f"{base}%"))).scalars())
    i = 1
    while f"{base}_{i}" in used:
        i += 1
    return f"{base}_{i}"


def _next_capability_id_free(db: Session) -> None:
    """占位：能力表用自增主键，插入时不用自己算 id。保留函数是为了让调用处对称可读。"""
    return None


# ============================================================ 增
def add_device(
    db: Session,
    *,
    preset_key: str,
    room_id: int,
    home_id: int | None = None,
    name: str | None = None,
    code: str | None = None,
) -> tuple[Device, Preset]:
    """按预设新增一台设备。返回 (设备, 预设)。

    `home_id` **留空时从房间推导**：房间本身挂在某套房下，再让调用方传一遍
    只会多出一个"传错就静默失败"的地方（多套房改造时就踩过：API 里写死
    `home_id=1`，导致新建的套房永远加不进设备，报"房间不属于家庭 1"）。
    传了则作为一致性校验。
    """
    preset = get_preset(preset_key)
    if preset is None:
        keys = "、".join(p.key for p in load_presets())
        raise DeviceAdminError(
            f"没有这个预设：{preset_key!r}。可选：{keys}",
            status_code=404,
        )

    from app.models.orm import Room  # 局部导入，避免模块级循环

    room = db.get(Room, room_id)
    if room is None:
        raise DeviceAdminError(f"房间 {room_id} 不存在", status_code=404)
    if home_id is not None and room.home_id != home_id:
        raise DeviceAdminError(
            f"房间 {room_id} 不属于家庭 {home_id}（它属于家庭 {room.home_id}）", status_code=404
        )
    # 唯一真相：设备跟着房间走
    home_id = room.home_id

    final_name = (name or "").strip() or f"{room.name}{preset.label}"
    final_code = (code or "").strip() or _next_device_code(db, preset.device_type)

    # 同样是全库查重：编码是目录/缓存的唯一 key，跨套房重名会让一台设备被覆盖
    dup = db.execute(select(Device).where(Device.code == final_code)).scalar_one_or_none()
    if dup is not None:
        raise DeviceAdminError(
            f"设备编码 {final_code!r} 已被占用（在套房 {dup.home_id}）", status_code=409
        )

    device = Device(
        home_id=home_id,
        room_id=room.id,
        code=final_code,
        name=final_name,
        device_type=preset.device_type,
        protocol="mqtt",
        # ⚠️ 唯一事实来源：按类型推导，不看请求、不看预设文件
        high_risk=1 if preset.high_risk else 0,
        enabled=1,
    )
    db.add(device)
    db.flush()  # 拿到 device.id

    for c in preset.capabilities:
        db.add(
            DeviceCapability(
                device_id=device.id,
                capability_code=c["code"],
                display_name=c["display_name"],
                value_type=c["value_type"],
                unit=c["unit"],
                value_min=c["value_min"],
                value_max=c["value_max"],
                step=c["step"],
                enum_values=c["enum_values"],
                writable=1 if c["writable"] else 0,
                risk_level=c["risk_level"],
            )
        )

    db.commit()
    db.refresh(device)
    log.info(
        "新增设备 id=%s code=%s type=%s 高风险=%s（预设 %s）",
        device.id, device.code, device.device_type, preset.high_risk, preset.key,
    )
    return device, preset


# ============================================================ 软删 / 恢复
def set_device_enabled(db: Session, device_id: int, enabled: bool) -> Device:
    """启停一台设备。

    ⚠️ 这是**软删**：只把 `enabled` 置 0。后端目录与生成器的 SQL 都带
    `WHERE d.enabled = 1`，所以设备会从接口和生成器里同时消失，且
    **历史数据、指令审计、场景引用全部保留**（硬删会被外键挡，且留下孤儿读数）。
    """
    device = db.get(Device, device_id)
    if device is None:
        raise DeviceAdminError(f"设备 {device_id} 不存在", status_code=404)

    device.enabled = 1 if enabled else 0
    db.commit()
    db.refresh(device)
    log.info("设备 %s(id=%s) 已%s", device.code, device.id, "启用" if enabled else "停用（软删）")
    return device


def list_disabled(db: Session, home_id: int) -> list[dict[str, Any]]:
    """列出已停用的设备。

    为什么需要它：`/api/devices` 走的是设备目录，而目录的 SQL 带 `WHERE d.enabled = 1`
    —— 所以**停用之后设备会从列表里消失**，用户再也点不到"恢复"。
    这个函数直接查库（绕过目录），把已停用的设备单独列出来。

    注意返回里**没有最新读数**：设备已停用，它的值不该再被当成有效状态展示。
    """
    from app.models.orm import Room  # 局部导入，避免模块级循环

    rows = db.execute(
        select(
            Device.id, Device.code, Device.name, Device.device_type,
            Device.high_risk, Room.name.label("room_name"),
        )
        .outerjoin(Room, Room.id == Device.room_id)
        .where(Device.home_id == home_id, Device.enabled == 0)
        .order_by(Device.id.desc())
    ).all()

    return [
        {
            "id": r.id,
            "code": r.code,
            "name": r.name,
            "device_type": r.device_type,
            "room_name": r.room_name or "",
            "high_risk": bool(r.high_risk),
        }
        for r in rows
    ]


# ============================================================ 彻底删除（硬删）
#: 硬删时**跳过**的表。目前为空；留着做口子（比如将来要保留某类归档表）。
PURGE_SKIP_TABLES: frozenset[str] = frozenset()


def device_child_tables(db: Session) -> list[str]:
    """查出所有带 `device_id` 列的表。

    ⚠️ 为什么**运行时查 information_schema**，而不是写死一张清单：
    写死的清单一定会忘。本项目已经有 **6 张**表带 `device_id`
    （`alarms` / `device_capabilities` / `device_commands` /
    `device_readings` / `energy_readings` / `sensor_events`）——
    少删一张就是外键 1451 或者留下孤儿数据（最初我凭印象以为是 3 张，实测是 6 张）。
    """
    rows = db.execute(
        text(
            """
            SELECT TABLE_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_NAME = 'device_id'
            ORDER BY TABLE_NAME
            """
        )
    ).scalars()

    out: list[str] = []
    for name in rows:
        n = str(name)
        if n in PURGE_SKIP_TABLES:
            continue
        # 表名来自 information_schema（可信），但它会被拼进 SQL，仍然做一次白名单校验
        if re.fullmatch(r"[A-Za-z0-9_]+", n):
            out.append(n)
    return out


def scenes_referencing(db: Session, device_code: str) -> list[dict[str, Any]]:
    """哪些已保存场景引用了这台设备（按 `device_code`，存在 `definition_json` 里）。

    ⚠️ 场景的动作是 JSON，**没有外键**。删掉设备后这些引用会变成悬空 ——
    好在执行时 planner 会重新编排、把不存在的设备剔除（`executor` 里有
    "设备目录中已不存在该设备" 的分支），所以不会崩，只是那个动作被丢掉。
    但**必须如实告诉用户**，否则他会以为场景还是完整的。
    """
    from app.models.orm import Scene  # 局部导入，避免模块级循环

    out: list[dict[str, Any]] = []
    for s in db.execute(select(Scene)).scalars():
        d = s.definition_json or {}
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:  # noqa: BLE001
                continue
        actions = (d or {}).get("actions") or []
        hit = [a for a in actions if isinstance(a, dict) and a.get("device_code") == device_code]
        if hit:
            out.append({"id": s.id, "name": s.name, "actions": len(hit)})
    return out


def purge_device(
    db: Session, device_id: int, *, acknowledge_audit_loss: bool = False
) -> dict[str, Any]:
    """**彻底删除**一台设备（不可逆）。

    三重保险（都不是形式主义，各自拦一类事故）：
    1. **必须先停用**。刚点完「停用」又立刻「彻底删除」，说明用户大概率没想清楚 ——
       强制多一步，给一个冷静期。也顺带保证"删掉的一定是不再用的设备"。
    2. **必须显式确认会丢审计**（`acknowledge_audit_loss=true`）。
       指令记录里存着高风险动作的"人工确认留痕"，删掉就没了 ——
       这是本项目唯一一处允许真正销毁审计数据的入口，所以要用户亲口确认。
    3. 高风险的**待确认项**也在被删之列。界面会额外提示，不能只靠这一点。

    返回一份**如实报告**：每张表删了多少行、哪些场景受了影响。
    """
    device = db.get(Device, device_id)
    if device is None:
        raise DeviceAdminError(f"设备 {device_id} 不存在", status_code=404)

    if device.enabled:
        raise DeviceAdminError(
            f"「{device.name}」还在启用中。彻底删除不可逆，请先点「停用」确认不再需要，再来删。",
            status_code=409,
        )

    if not acknowledge_audit_loss:
        raise DeviceAdminError(
            "彻底删除会连「指令审计记录」一起删掉（高风险动作的人工确认留痕也在里面），"
            "且不可恢复。请显式确认 acknowledge_audit_loss=true 再来。",
            status_code=400,
        )

    affected = scenes_referencing(db, device.code)
    tables = device_child_tables(db)

    deleted: dict[str, int] = {}
    for t in tables:
        res = db.execute(text(f"DELETE FROM {t} WHERE device_id = :did"), {"did": device_id})
        deleted[t] = int(res.rowcount or 0)

    info = {
        "id": device.id,
        "code": device.code,
        "name": device.name,
        "device_type": device.device_type,
        "high_risk": bool(device.high_risk),
        "room_id": device.room_id,
    }

    db.delete(device)
    db.commit()

    total = sum(deleted.values())
    log.warning(
        "⚠️ 彻底删除设备 %s(id=%s)：共清理 %d 行%s；受影响场景 %d 个",
        info["code"], info["id"], total,
        "".join(f"，{k} {v}" for k, v in deleted.items() if v),
        len(affected),
    )

    return {**info, "deleted": deleted, "total_rows": total, "affected_scenes": affected}


def build_purge_message(report: dict[str, Any]) -> str:
    """把删除报告拼成一句人话（界面直接显示，不用前端自己组织措辞）。"""
    detail = "、".join(f"{k} {v} 行" for k, v in report["deleted"].items() if v) or "无附加数据"

    msg = f"已彻底删除「{report['name']}」（{report['code']}），清理了 {detail}。"
    if report["affected_scenes"]:
        names = "、".join(
            f"{s['name']}（{s['actions']} 个动作）" for s in report["affected_scenes"][:3]
        )
        more = f" 等 {len(report['affected_scenes'])} 个" if len(report["affected_scenes"]) > 3 else ""
        msg += f" ⚠️ 有 {len(report['affected_scenes'])} 个场景引用了它，执行时会自动跳过这些动作：{names}{more}。"
    return msg
