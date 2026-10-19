"""套房的增删（前端「新建套房 / 删除套房」的后端）。

为什么不复用 `scripts/seed_second_home.py`
----------------------------------------
那个脚本是**给开发者用的**（写死一套演示房、直接连库）。这里要的是
**任意名字、任意房间名**，而且必须做校验（重名、空名字、房间过多），
还要在删除时按外键顺序级联清理。两者的输入与约束完全不同，
硬塞进一个函数会让脚本也跟着变复杂。

删除为什么这么长
----------------
数据库的外键是 RESTRICT（没有 CASCADE），所以必须**从叶子往根删**：
读数/事件/指令 → 设备能力 → 设备 → 照护观察/偏好 → 成员 → 场景运行 → 场景
→ 建议/记忆/对话/干扰规则 → 房间 → 套房。
顺序错一步就会被外键挡住，报一个看不出所以然的 1451。

⚠️ 套房里的设备历史数据会被真的删掉，没有回收站 —— 所以 API 层要求显式确认，
   并且**不允许删掉最后一套房**（否则前端所有页面都会因为 404 变成空白）。
"""

from __future__ import annotations

import logging
import re
import shutil
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config.settings import PROJECT_ROOT

log = logging.getLogger("app.services.homes_admin")

#: 户型的配置文件目录（与 `floorplan.py` 用同一个常量，避免两处各算一遍路径）
PLAN_DIR = PROJECT_ROOT / "config" / "floorplans"

MAX_NAME_LEN = 64
MAX_ROOMS = 24
#: ⚠️ 与 `floorplan.py:MAX_NAME_LEN` 保持一致：户型保存时按 24 校验房间名，
#: 这里放宽到 32 就会出现"建得出来、但户型图保存不了"的死角。
MAX_ROOM_NAME_LEN = 24


class HomeAdminError(Exception):
    """带 HTTP 状态码的业务错误（与 `DeviceAdminError` 保持一致的风格）。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _clean_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip())
    if not n:
        raise HomeAdminError(400, "套房名称不能为空")
    if len(n) > MAX_NAME_LEN:
        raise HomeAdminError(400, f"套房名称最多 {MAX_NAME_LEN} 个字符")
    return n


def _clean_rooms(room_names: list[str]) -> list[str]:
    """清洗房间名：去空白、去重（保持顺序）、限制数量。

    房间名是**用户自己起的**，允许中文/空格/标点；只要求非空且不重复。
    房间**编码**由后端生成（见 `_room_codes`），因为编码要进 MQTT topic，
    不能让用户输入带 `/` `+` `#` 之类的通配字符。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in room_names or []:
        n = re.sub(r"\s+", " ", (raw or "").strip())
        if not n:
            continue
        if len(n) > MAX_ROOM_NAME_LEN:
            raise HomeAdminError(400, f"房间名「{n[:8]}…」超过 {MAX_ROOM_NAME_LEN} 个字符")
        if n in seen:
            raise HomeAdminError(400, f"已有同名房间「{n}」，请改成不同的名字")
        seen.add(n)
        out.append(n)
    # ⚠️ **允许 0 个房间**：新套房可以先空着，房间到户型编辑器里画框命名时创建。
    if len(out) > MAX_ROOMS:
        raise HomeAdminError(400, f"一套房最多 {MAX_ROOMS} 个房间")
    return out


def _room_codes(count: int) -> list[str]:
    """生成房间编码：`room_1` … `room_n`。

    刻意**不从中文房间名转拼音**：转拼音要额外依赖，而且同音字/多音字会撞码。
    编码只承担"进 topic 唯一标识"的职责，显示名另有 `rooms.name`。
    """
    return [f"room_{i}" for i in range(1, count + 1)]


def create_home(
    db: Session, *, name: str, room_names: list[str], timezone: str = "Asia/Shanghai"
) -> dict[str, Any]:
    """建一套房 + 它的房间。返回 `{home_id, name, rooms}`。"""
    name = _clean_name(name)
    rooms = _clean_rooms(room_names)

    dup = db.execute(text("SELECT id FROM homes WHERE name = :n"), {"n": name}).first()
    if dup:
        raise HomeAdminError(409, f"已经有一套叫「{name}」的房子了，换个名字吧")

    db.execute(
        text(
            "INSERT INTO homes (name, timezone, address_hash) "
            "VALUES (:n, :tz, NULL)"
        ),
        {"n": name, "tz": timezone or "Asia/Shanghai"},
    )
    db.flush()
    home_id = int(
        db.execute(text("SELECT id FROM homes WHERE name = :n"), {"n": name}).scalar_one()
    )

    codes = _room_codes(len(rooms))
    for i, (code, rname) in enumerate(zip(codes, rooms), start=1):
        db.execute(
            text(
                "INSERT INTO rooms (home_id, code, name, floor, sort_no) "
                "VALUES (:h, :c, :n, 1, :s)"
            ),
            {"h": home_id, "c": code, "n": rname, "s": i},
        )
    db.commit()

    log.info("新建套房 id=%s「%s」，房间 %d 间", home_id, name, len(rooms))
    return {
        "home_id": home_id,
        "name": name,
        "rooms": [{"code": c, "name": n} for c, n in zip(codes, rooms)],
    }


def delete_home(db: Session, home_id: int) -> dict[str, Any]:
    """删除一套房及其全部数据。返回被删掉的数量，用于给用户一个交代。"""
    row = db.execute(text("SELECT id, name FROM homes WHERE id = :h"), {"h": home_id}).first()
    if row is None:
        raise HomeAdminError(404, f"套房 {home_id} 不存在")

    total = int(db.execute(text("SELECT COUNT(*) FROM homes")).scalar() or 0)
    if total <= 1:
        raise HomeAdminError(400, "这是最后一套房，删掉之后所有页面都会没数据，不能删")

    counts: dict[str, int] = {}

    def _del(sql: str) -> int:
        """执行一条删除并返回行数。

        ⚠️ **一律用 `device_id IN (SELECT …)` 批量删，不要按设备循环。**
        一号房有 328 万行读数，逐台设备删（32 条 DELETE）会让一次请求卡几分钟，
        前端看着像"点删除没反应"，用户很可能重复点。
        批量删是一条语句走索引，同样是全量删，但快一个量级。
        """
        return int(db.execute(text(sql), {"h": home_id}).rowcount or 0)

    # ---- 设备叶子：读数 / 事件 / 指令 / 能耗 / 能力 ----
    counts["device_readings"] = _del(
        "DELETE FROM device_readings WHERE device_id IN "
        "(SELECT id FROM devices WHERE home_id = :h)"
    )
    counts["sensor_events"] = _del(
        "DELETE FROM sensor_events WHERE device_id IN "
        "(SELECT id FROM devices WHERE home_id = :h)"
    )
    counts["device_commands"] = _del(
        "DELETE FROM device_commands WHERE device_id IN "
        "(SELECT id FROM devices WHERE home_id = :h)"
    )
    counts["energy_readings"] = _del(
        "DELETE FROM energy_readings WHERE device_id IN "
        "(SELECT id FROM devices WHERE home_id = :h)"
    )
    counts["device_capabilities"] = _del(
        "DELETE FROM device_capabilities WHERE device_id IN "
        "(SELECT id FROM devices WHERE home_id = :h)"
    )

    # ---- 告警：按设备或房间归属。⚠️ 没有外键，但不清会留下"孤儿告警" ----
    counts["alarms"] = _del(
        "DELETE FROM alarms WHERE device_id IN (SELECT id FROM devices WHERE home_id = :h) "
        "OR room_id IN (SELECT id FROM rooms WHERE home_id = :h)"
    )

    # ---- 场景：先删运行记录，再删场景 ----
    _del("DELETE FROM scene_runs WHERE scene_id IN (SELECT id FROM scenes WHERE home_id = :h)")

    # ---- 成员：照护观察 / 偏好都指向成员 ----
    counts["care_observations"] = _del(
        "DELETE FROM care_observations WHERE member_id IN "
        "(SELECT id FROM members WHERE home_id = :h)"
    )
    _del(
        "DELETE FROM member_preferences WHERE member_id IN "
        "(SELECT id FROM members WHERE home_id = :h)"
    )

    # ---- 用户：先删角色关联（users.member_id 指向成员，所以成员要在用户之后删）----
    _del("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE home_id = :h)")
    _del("DELETE FROM users WHERE home_id = :h")

    # ---- 直接按 home_id 挂着的表 ----
    counts["suggestions"] = _del("DELETE FROM suggestions WHERE home_id = :h")
    counts["memories"] = _del("DELETE FROM memories WHERE home_id = :h")
    # 对话正文（chat_messages）挂在会话下面，没有外键，得先按 thread_id 清掉，
    # 否则删完套房会留下一堆没人认领的历史消息
    _del(
        "DELETE FROM chat_messages WHERE thread_id IN "
        "(SELECT thread_id FROM conversations WHERE home_id = :h)"
    )
    counts["conversations"] = _del("DELETE FROM conversations WHERE home_id = :h")
    counts["disturbance_rules"] = _del("DELETE FROM disturbance_rules WHERE home_id = :h")
    _del("DELETE FROM scenes WHERE home_id = :h")
    _del("DELETE FROM members WHERE home_id = :h")

    # ---- 设备 / 房间 / 套房 ----
    counts["devices"] = _del("DELETE FROM devices WHERE home_id = :h")
    counts["rooms"] = _del("DELETE FROM rooms WHERE home_id = :h")
    _del("DELETE FROM homes WHERE id = :h")
    db.commit()

    removed_plan = _remove_floorplan(home_id)
    log.info("已删除套房 id=%s「%s」：%s", home_id, row[1], counts)
    return {"home_id": home_id, "name": row[1], "deleted": counts, "floorplan_removed": removed_plan}


def _remove_floorplan(home_id: int) -> bool:
    """删掉这套房的户型文件与底图。留着的话下次建房用同一个 id 会"继承"旧户型。"""
    removed = False
    plan = PLAN_DIR / f"{home_id}.json"
    if plan.exists():
        plan.unlink()
        removed = True
    asset_dir = PLAN_DIR / f"{home_id}"
    if asset_dir.is_dir():
        shutil.rmtree(asset_dir, ignore_errors=True)
        removed = True
    return removed


def create_room(db: Session, home_id: int, name: str) -> dict[str, Any]:
    """在已有套房里新增一个房间。

    为什么房间要在户型编辑器里加
    --------------------------
    房间"叫什么"和"画在哪"是同一件事：用户拖着框说"这间是书房"，
    名字才有意义。所以新建套房只给几个占位房名，真正的命名发生在这里。

    ⚠️ 编码仍由后端生成（`room_N`），不接收用户输入：编码要进 MQTT topic。
    """
    home = db.execute(text("SELECT id FROM homes WHERE id = :h"), {"h": home_id}).first()
    if home is None:
        raise HomeAdminError(404, f"套房 {home_id} 不存在")

    clean = re.sub(r"\s+", " ", (name or "").strip())
    if not clean:
        raise HomeAdminError(400, "房间名不能为空")
    if len(clean) > MAX_ROOM_NAME_LEN:
        raise HomeAdminError(400, f"房间名最多 {MAX_ROOM_NAME_LEN} 个字符")

    rows = db.execute(
        text("SELECT code, name FROM rooms WHERE home_id = :h ORDER BY id"), {"h": home_id}
    ).all()
    if len(rows) >= MAX_ROOMS:
        raise HomeAdminError(400, f"一套房最多 {MAX_ROOMS} 个房间")
    # 房间名必须唯一（走廊是交通空间，不建房间记录，所以不受这条限制）
    if any(r[1] == clean for r in rows):
        raise HomeAdminError(409, f"已有同名房间「{clean}」，换个名字（比如「主卫」）")

    # 找下一个可用的 room_N（删除过的编码会被重新利用，但不会撞上现有编码）
    used = {str(r[0]) for r in rows}
    n = 1
    while f"room_{n}" in used:
        n += 1
    code = f"room_{n}"
    sort_no = max(
        [int(r[0].split("_")[-1]) for r in rows if str(r[0]).startswith("room_")] or [0]
    ) + 1

    db.execute(
        text(
            "INSERT INTO rooms (home_id, code, name, floor, sort_no) "
            "VALUES (:h, :c, :n, 1, :s)"
        ),
        {"h": home_id, "c": code, "n": clean, "s": sort_no},
    )
    db.commit()
    room_id = int(
        db.execute(
            text("SELECT id FROM rooms WHERE home_id = :h AND code = :c"),
            {"h": home_id, "c": code},
        ).scalar_one()
    )
    log.info("套房 %s 新增房间 id=%s「%s」(code=%s)", home_id, room_id, clean, code)
    return {"room_id": room_id, "code": code, "name": clean}


def rename_home(db: Session, home_id: int, name: str) -> dict[str, Any]:
    """给套房改名。

    和新建走同一套校验（去空白 / 长度 / 重名），避免"建的时候拦、改的时候放过去"。
    """
    row = db.execute(text("SELECT id, name FROM homes WHERE id = :h"), {"h": home_id}).first()
    if row is None:
        raise HomeAdminError(404, f"套房 {home_id} 不存在")

    clean = _clean_name(name)
    if clean == row[1]:
        # 没改就别写库，也别报重名（否则改成一个字没动会莫名其妙 409）
        return {"home_id": home_id, "name": clean, "changed": False}

    dup = db.execute(
        text("SELECT id FROM homes WHERE name = :n AND id <> :h"), {"n": clean, "h": home_id}
    ).first()
    if dup:
        raise HomeAdminError(409, f"已经有一套叫「{clean}」的房子了，换个名字吧")

    db.execute(text("UPDATE homes SET name = :n WHERE id = :h"), {"n": clean, "h": home_id})
    db.commit()
    log.info("套房 %s 改名：「%s」→「%s」", home_id, row[1], clean)
    return {"home_id": home_id, "name": clean, "changed": True}


def delete_room(db: Session, home_id: int, code: str) -> dict[str, Any]:
    """删除一个房间，**连同它的设备与全部历史数据**。

    为什么要连设备一起删
    ------------------
    房间是设备的归属。留着设备会挂在"不存在的房间"上：房间卡片没有它、
    按房间过滤查不到、告警也没有落脚点 —— 用户只能去设备页一台台找。

    ⚠️ 与 `purge_device` 的"先停用 + 确认审计丢失"不同：这里是**房间为单位的整体删除**，
       界面在删框时已经用确认框把"房间 + N 台设备 + 历史数据"说清楚了。
    """
    row = db.execute(
        text("SELECT id, code, name FROM rooms WHERE home_id = :h AND code = :c"),
        {"h": home_id, "c": code},
    ).first()
    if row is None:
        raise HomeAdminError(404, f"房间 {code!r} 不存在（或不属于套房 {home_id}）")

    room_id, room_code, room_name = int(row[0]), str(row[1]), str(row[2])
    dev_ids = [
        int(r[0])
        for r in db.execute(text("SELECT id FROM devices WHERE room_id = :r"), {"r": room_id}).all()
    ]

    # 复用设备那边的子表清单（运行时查 information_schema，避免漏表）
    from app.services.device_admin import device_child_tables

    deleted: dict[str, int] = {}
    for t in device_child_tables(db):
        res = db.execute(
            text(f"DELETE FROM {t} WHERE device_id IN (SELECT id FROM devices WHERE room_id = :r)"),
            {"r": room_id},
        )
        if res.rowcount:
            deleted[t] = int(res.rowcount)

    # 只挂在房间上、没挂设备的告警
    res = db.execute(text("DELETE FROM alarms WHERE room_id = :r"), {"r": room_id})
    if res.rowcount:
        deleted["alarms"] = deleted.get("alarms", 0) + int(res.rowcount)

    db.execute(text("DELETE FROM devices WHERE room_id = :r"), {"r": room_id})
    db.execute(text("DELETE FROM rooms WHERE id = :r"), {"r": room_id})
    db.commit()

    log.warning(
        "⚠️ 删除房间 %s「%s」(id=%s)：设备 %d 台，清理 %s",
        room_code, room_name, room_id, len(dev_ids),
        "、".join(f"{k} {v}" for k, v in deleted.items()) or "无历史数据",
    )
    return {
        "room_id": room_id,
        "code": room_code,
        "name": room_name,
        "devices": len(dev_ids),
        "deleted": deleted,
    }
