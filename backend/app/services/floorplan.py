"""户型图几何：**按套房（home）隔离**读写 `config/floorplans/{home_id}.json`。

为什么存文件而不是数据库
--------------------
1. `rooms` 表**没有任何几何字段**，要加就得改 DDL，而 `app_rw` 没有 DDL 权限
   （本项目既定约束：DDL 只能在虚拟机里手工执行）；
2. 户型几何是"一份配置"，不是随时间增长的业务数据 —— 存文件天然带版本、能进 git；
3. 前端读它、人也能读它，出错时肉眼就能看出坐标写错了。

与数据库的关系（两条硬约束）
--------------------------
1. `areas[].code` **必须能对上 `rooms.code`**（它进 MQTT topic，写错就等于把读数
   挂到了不存在的房间上）。保存时一定拿 `rooms` 表校验，不能只信前端。
   交通空间（走廊/通道）例外：`rooms` 表里没有它，允许 `is_circulation=true`
   的编码不在库中，但它不能挂设备、只作视觉过渡。

2. **房间显示名只有一个真相：`rooms.name`**。用户在界面上改名，这里会写回
   `rooms` 表（普通 UPDATE，不需要 DDL），这样设备页、照护页、房间卡片看到的是
   同一个名字，不会出现"户型图上叫书房、设备页里还叫次卧"。

⚠️ 改完名字后调用方要**就地刷新设备目录**（`catalog.refresh(engine)`），
   否则接口读到的还是旧名字 —— 和"加了设备却看不见"是同一类坑。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config.settings import PROJECT_ROOT

log = logging.getLogger(__name__)

CONFIG_DIR = PROJECT_ROOT / "config"
PLAN_DIR = CONFIG_DIR / "floorplans"

#: 画布边长上限（用户单位）。防止一张 40000 像素的图把 SVG 坐标算到失真。
MAX_VIEW_SIZE = 6000
#: 单个房间最小边长（用户单位）。太小了根本点不中。
MIN_AREA_SIZE = 12.0
#: 房间名长度上限（与 `rooms.name varchar(48)` 对齐，留点余量）
MAX_NAME_LEN = 24
#: 房间类型长度上限（自由文本，只用于展示）
MAX_TYPE_LEN = 24
#: 底图大小上限（字节）
MAX_IMAGE_BYTES = 8 * 1024 * 1024
#: 允许的图片格式（**刻意不收 SVG**：SVG 能带脚本，而这张图会被 <image> 引用渲染）
ALLOWED_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP")

DEFAULT_VIEW_BOX = {"x": 5, "y": 5, "w": 628, "h": 395}

#: 默认几何（与前端 `roomGeometry.js` 里的 AREAS 一致）。
#: ⚠️ 只适用于 home_id=1（演示家庭）。别的套房没有"内置户型"可言，
#:    第一次进来应该是空画布 + 提示去编辑，而不是套用别人家的房间。
DEFAULT_AREAS: list[dict[str, Any]] = [
    {"code": "balcony", "name": "阳台", "x": 20, "y": 20, "w": 118, "h": 170},
    {"code": "living_room", "name": "客厅", "x": 138, "y": 20, "w": 214, "h": 170},
    {"code": "entryway", "name": "玄关", "x": 352, "y": 95, "w": 105, "h": 95},
    {"code": "kitchen", "name": "厨房", "x": 457, "y": 20, "w": 161, "h": 170},
    {
        "code": "hallway", "name": "通道", "x": 100, "y": 190, "w": 464, "h": 42,
        "is_circulation": True,
    },
    {"code": "master_bedroom", "name": "主卧", "x": 100, "y": 232, "w": 168, "h": 153},
    {"code": "bathroom", "name": "卫生间", "x": 268, "y": 232, "w": 106, "h": 153},
    {"code": "second_bedroom", "name": "次卧", "x": 374, "y": 232, "w": 190, "h": 153},
]


class FloorplanError(ValueError):
    """几何不合法（调用方转 400）。"""

    def __init__(self, message: str, *, fields: list[str] | None = None):
        super().__init__(message)
        self.fields = fields or []


@dataclasses.dataclass(frozen=True)
class RoomBrief:
    code: str
    name: str
    device_count: int


# ============================================================================
# 路径（按套房隔离）
# ============================================================================
def plan_path(home_id: int) -> Path:
    return PLAN_DIR / f"{home_id}.json"


def asset_dir(home_id: int) -> Path:
    return PLAN_DIR / str(home_id)


def background_path(home_id: int) -> Path | None:
    p = asset_dir(home_id) / "base.png"
    return p if p.exists() else None


def _rooms(db: Session, home_id: int) -> dict[str, RoomBrief]:
    rows = db.execute(
        text(
            """
            SELECT r.code, r.name,
                   (SELECT COUNT(*) FROM devices d
                     WHERE d.room_id = r.id AND d.enabled = 1) AS device_count
            FROM rooms r
            WHERE r.home_id = :hid
            ORDER BY r.sort_no, r.id
            """
        ),
        {"hid": home_id},
    ).all()
    return {r.code: RoomBrief(code=r.code, name=r.name, device_count=int(r.device_count)) for r in rows}


# ============================================================================
# 读
# ============================================================================
def _read_raw(home_id: int) -> dict[str, Any] | None:
    path = plan_path(home_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # 文件坏了不能让整个总览页挂掉：退回默认/空几何，但要留下明确的日志
        log.warning("floorplans/%s.json 读取失败（%s），本次按未自定义处理", home_id, e)
        return None


def load_floorplan(db: Session, home_id: int = 1) -> dict[str, Any]:
    """返回前端的完整几何。三种来源：custom（有文件）/ default（内置）/ empty（空画布）。"""
    raw = _read_raw(home_id)
    rooms = _rooms(db, home_id)

    if raw is not None:
        source = "custom"
        data = raw
    elif home_id == 1:
        source = "default"
        data = {"view_box": dict(DEFAULT_VIEW_BOX), "areas": [dict(a) for a in DEFAULT_AREAS]}
    else:
        # 别的套房没有内置户型：给空画布，让用户自己框。
        # 套用 home 1 的房间名会画出"别人家"的户型，比空着更容易误导。
        source = "empty"
        data = {"view_box": {"x": 0, "y": 0, "w": 900, "h": 600}, "areas": []}

    areas = []
    for a in data.get("areas") or []:
        code = str(a.get("code") or "")
        room = rooms.get(code)
        circ = bool(a.get("is_circulation"))
        areas.append(
            {
                "code": code,
                # 房间名以数据库为准（改名走 rooms.name 这一条路）
                "name": (room.name if room else str(a.get("name") or code)),
                "type": str(a.get("type") or ""),
                "x": float(a.get("x") or 0),
                "y": float(a.get("y") or 0),
                "w": float(a.get("w") or 0),
                "h": float(a.get("h") or 0),
                "is_circulation": circ,
                "known_room": room is not None,
            }
        )

    drawn = {a["code"] for a in areas if not a["is_circulation"]}
    missing = [
        {"code": r.code, "name": r.name, "device_count": r.device_count}
        for code, r in rooms.items()
        if code not in drawn
    ]

    bg = data.get("background") if isinstance(data.get("background"), dict) else None
    bg_file = background_path(home_id)
    has_bg = bool(bg) and bg_file is not None
    updated_at = data.get("updated_at")

    return {
        "home_id": home_id,
        "source": source,
        "is_custom": source == "custom",
        "updated_at": updated_at,
        "view_box": {
            "x": float((data.get("view_box") or DEFAULT_VIEW_BOX).get("x", 0)),
            "y": float((data.get("view_box") or DEFAULT_VIEW_BOX).get("y", 0)),
            "w": float((data.get("view_box") or DEFAULT_VIEW_BOX).get("w", 100)),
            "h": float((data.get("view_box") or DEFAULT_VIEW_BOX).get("h", 100)),
        },
        "areas": areas,
        "background": (
            {
                "url": f"/api/floorplan/image?home_id={home_id}&v={_cache_token(updated_at)}",
                "width": int(bg.get("width") or 0),
                "height": int(bg.get("height") or 0),
                "opacity": float(bg.get("opacity", 0.9)),
            }
            if has_bg
            else None
        ),
        # 没画出来的房间：前端可以提示"这些房间的设备不会出现在户型图上"
        "missing_rooms": missing,
    }


def _cache_token(updated_at: str | None) -> str:
    """给图片 URL 加个版本号，避免换了底图浏览器还用缓存。"""
    if not updated_at:
        return "0"
    try:
        ts = dt.datetime.fromisoformat(updated_at)
        return str(int(ts.timestamp()))
    except ValueError:
        return "0"


# ============================================================================
# 写（带校验）
# ============================================================================
def _num(v: Any, field: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise FloorplanError(f"{field} 必须是数字", fields=[field]) from e
    if f != f or f in (float("inf"), float("-inf")):
        raise FloorplanError(f"{field} 不是合法数值", fields=[field])
    return f


def validate_payload(
    db: Session, payload: dict[str, Any], home_id: int = 1
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """校验并规整保存内容。返回 (规整后的几何, 需要改名的房间 [(code, new_name)])。

    **不接受前端说是合法就信**：房间码要查库、尺寸要够大、名字要去空白。
    """
    rooms = _rooms(db, home_id)
    vb = payload.get("view_box") or {}
    view_box = {
        "x": _num(vb.get("x", 0), "view_box.x"),
        "y": _num(vb.get("y", 0), "view_box.y"),
        "w": _num(vb.get("w", 0), "view_box.w"),
        "h": _num(vb.get("h", 0), "view_box.h"),
    }
    if view_box["w"] <= 0 or view_box["h"] <= 0:
        raise FloorplanError("画布宽高必须大于 0", fields=["view_box"])
    if view_box["w"] > MAX_VIEW_SIZE or view_box["h"] > MAX_VIEW_SIZE:
        raise FloorplanError(f"画布边长不能超过 {MAX_VIEW_SIZE}", fields=["view_box"])

    raw_areas = payload.get("areas")
    if not isinstance(raw_areas, list):
        raise FloorplanError("areas 必须是数组", fields=["areas"])
    # ⚠️ **允许空数组**：把最后一个房间框删掉是合法操作（要连房间一起删）。
    #    前端仍会拦"一个房间都没有"的普通保存，只有"删框后保存"才走得通。

    seen: set[str] = set()
    areas: list[dict[str, Any]] = []
    renames: list[tuple[str, str]] = []

    for i, a in enumerate(raw_areas):
        if not isinstance(a, dict):
            raise FloorplanError(f"第 {i + 1} 个区域格式不对", fields=[f"areas[{i}]"])
        code = str(a.get("code") or "").strip()
        if not code:
            raise FloorplanError(f"第 {i + 1} 个区域没有指定房间", fields=[f"areas[{i}].code"])
        if code in seen:
            raise FloorplanError(f"房间 {code} 被画了两次", fields=[f"areas[{i}].code"])
        circ = bool(a.get("is_circulation"))

        if code not in rooms and not circ:
            raise FloorplanError(
                f"房间编码 {code!r} 在 rooms 表里不存在（只有交通空间可以不在库中）",
                fields=[f"areas[{i}].code"],
            )
        if circ and code in rooms:
            raise FloorplanError(
                f"{code!r} 是真实房间，不能标成交通空间", fields=[f"areas[{i}].is_circulation"]
            )

        x = _num(a.get("x"), f"areas[{i}].x")
        y = _num(a.get("y"), f"areas[{i}].y")
        w = _num(a.get("w"), f"areas[{i}].w")
        h = _num(a.get("h"), f"areas[{i}].h")
        if w < MIN_AREA_SIZE or h < MIN_AREA_SIZE:
            raise FloorplanError(
                f"房间 {code} 太小（宽高至少 {MIN_AREA_SIZE:.0f}）", fields=[f"areas[{i}]"]
            )

        # ---- 类型：自由文本，只用于展示；不参与唯一约束 ----
        raw_type = str(a.get("type") or "").strip()
        if len(raw_type) > MAX_TYPE_LEN:
            raise FloorplanError(
                f"房间类型太长（最多 {MAX_TYPE_LEN} 字）", fields=[f"areas[{i}].type"]
            )

        # ---- 名字：真实房间写回 rooms.name；交通空间只存在几何文件里 ----
        raw_name = str(a.get("name") or "").strip()
        if len(raw_name) > MAX_NAME_LEN:
            raise FloorplanError(
                f"房间名太长（最多 {MAX_NAME_LEN} 字）", fields=[f"areas[{i}].name"]
            )
        if code in rooms:
            final_name = raw_name or rooms[code].name
            if final_name != rooms[code].name:
                renames.append((code, final_name))
        else:
            if not raw_name:
                raise FloorplanError(
                    f"交通空间 {code!r} 需要一个名字（如「走廊」）", fields=[f"areas[{i}].name"]
                )
            final_name = raw_name

        seen.add(code)
        areas.append(
            {
                "code": code,
                "name": final_name,
                "type": raw_type,
                "x": round(x, 2),
                "y": round(y, 2),
                "w": round(w, 2),
                "h": round(h, 2),
                "is_circulation": circ,
            }
        )

    # ---- 房间名唯一性：走廊以外不许重名（走廊可以有"走廊 A / 走廊 B"甚至两个"走廊"）----
    # 注意要把"没画到图上的房间"也算进来：它们仍然占着名字。
    final_names: dict[str, str] = {code: r.name for code, r in rooms.items()}
    for a in areas:
        if not a["is_circulation"]:
            final_names[a["code"]] = a["name"]
    owners: dict[str, list[str]] = {}
    for code, nm in final_names.items():
        owners.setdefault(nm, []).append(code)
    dup = {nm: codes for nm, codes in owners.items() if len(codes) > 1}
    if dup:
        nm, codes = next(iter(dup.items()))
        raise FloorplanError(
            f"房间名「{nm}」重复了（{ '、'.join(codes) }）。走廊以外的房间名必须唯一，"
            "请改成一个不同的名字",
            fields=["areas"],
        )

    return {"view_box": view_box, "areas": areas}, renames


def _apply_renames(db: Session, home_id: int, renames: list[tuple[str, str]]) -> None:
    """把自定义房间名写回 `rooms.name`（唯一真相），这样全站显示一致。"""
    for code, name in renames:
        db.execute(
            text("UPDATE rooms SET name = :n WHERE home_id = :h AND code = :c"),
            {"n": name[:48], "h": home_id, "c": code},
        )
    if renames:
        db.commit()
        log.info("房间改名：%s", "、".join(f"{c}→{n}" for c, n in renames))


def save_floorplan(db: Session, payload: dict[str, Any], home_id: int = 1) -> dict[str, Any]:
    cleaned, renames = validate_payload(db, payload, home_id)
    raw = _read_raw(home_id) or {}
    bg = raw.get("background") if isinstance(raw.get("background"), dict) else None

    # 底图存在时，画布必须与图片像素尺寸一致 —— 否则前端框选的坐标与底图对不上
    if bg and background_path(home_id) is not None:
        if (
            cleaned["view_box"]["w"] != float(bg.get("width") or 0)
            or cleaned["view_box"]["h"] != float(bg.get("height") or 0)
        ):
            raise FloorplanError(
                "已上传底图时，画布尺寸必须与底图一致（前端应直接提交图片尺寸）",
                fields=["view_box"],
            )

    _apply_renames(db, home_id, renames)

    doc = {
        "version": 1,
        "home_id": home_id,
        "updated_at": dt.datetime.now().replace(microsecond=0).isoformat(),
        **cleaned,
        "background": bg,
    }
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    path = plan_path(home_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)                    # 先写临时文件再改名，避免读到半个 JSON
    log.info("套房 %s 的户型已保存：%d 个区域", home_id, len(doc["areas"]))
    return load_floorplan(db, home_id)


# ============================================================================
# 底图
# ============================================================================
def save_background(db: Session, data: bytes, home_id: int = 1) -> dict[str, Any]:
    """校验并保存底图，同时把画布尺寸设成图片尺寸。

    ⚠️ 为什么要用 Pillow 重新编码而不是原样存：
    ① 确认它**真的是图片**（不信 content-type，也不信扩展名）；
    ② 顺带把 EXIF（可能含拍摄地点）与奇怪的颜色模式清掉；
    ③ 统一成 PNG，前端只处理一种格式。
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise FloorplanError(f"图片太大（上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB）")
    if not data:
        raise FloorplanError("上传内容为空")

    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise FloorplanError("服务端缺少 Pillow，无法校验图片") from e

    try:
        probe = Image.open(io.BytesIO(data))
        fmt = (probe.format or "").upper()
        if fmt not in ALLOWED_IMAGE_FORMATS:
            raise FloorplanError(f"不支持的图片格式 {fmt or '未知'}（仅支持 PNG / JPEG / WEBP）")
        probe.load()
        img = probe.convert("RGBA" if probe.mode in ("RGBA", "LA", "P") else "RGB")
    except FloorplanError:
        raise
    except Exception as e:  # noqa: BLE001
        raise FloorplanError(f"这个文件不是有效图片：{e}") from e

    w, h = img.size
    if w < 200 or h < 200:
        raise FloorplanError(f"图片太小（{w}×{h}），户型图至少要 200×200")
    if max(w, h) > MAX_VIEW_SIZE:
        # 统一缩到长边 2000，避免超大图让 SVG 坐标失真、前端也卡
        ratio = 2000.0 / max(w, h)
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))))
        w, h = img.size
        log.info("底图过大，已缩放到 %d×%d", w, h)

    adir = asset_dir(home_id)
    adir.mkdir(parents=True, exist_ok=True)
    img.save(adir / "base.png", format="PNG", optimize=True)

    raw = _read_raw(home_id) or {}
    raw["background"] = {"width": int(w), "height": int(h), "opacity": 0.9}
    raw["view_box"] = {"x": 0, "y": 0, "w": int(w), "h": int(h)}
    # ⚠️ 换底图 = 换坐标系，旧的框选必然错位 → **清空**，由用户在新图上重画。
    #    保留旧框看起来"没丢东西"，实际全是错的，比空着更糟。
    raw["areas"] = []
    raw["version"] = raw.get("version") or 1
    raw["home_id"] = home_id
    raw["updated_at"] = dt.datetime.now().replace(microsecond=0).isoformat()

    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    plan_path(home_id).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("套房 %s 的户型底图已保存：%d×%d", home_id, w, h)
    return load_floorplan(db, home_id)


def remove_background(db: Session, home_id: int = 1) -> dict[str, Any]:
    """删除底图，**只删图**：框选、房间、画布尺寸全部保留。

    为什么不像 `save_background` 那样清框
    ------------------------------------
    换图时坐标系变了（画布尺寸 = 新图尺寸），旧框必然错位；但"只删图"不动坐标系，
    框的位置仍然有效，清掉它们等于白白让用户重画一遍。

    删完之后前端会回到"无底图模式"：画出墙线、房间名回到中心、填充变实。
    """
    bg_file = background_path(home_id)
    raw = _read_raw(home_id) or {}
    has_meta = isinstance(raw.get("background"), dict)
    if bg_file is None and not has_meta:
        raise FloorplanError("这套房没有底图")

    if bg_file is not None:
        bg_file.unlink(missing_ok=True)

    if raw:
        raw.pop("background", None)
        # 画布尺寸保持原样（view_box 不动）—— 这正是"框不会错位"的原因
        raw["updated_at"] = dt.datetime.now().replace(microsecond=0).isoformat()
        plan_path(home_id).write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    log.info("套房 %s 的底图已删除（框选与房间保留）", home_id)
    return load_floorplan(db, home_id)


def reset_floorplan(db: Session, home_id: int = 1, *, keep_background: bool = False) -> dict[str, Any]:
    """恢复默认几何。

    `keep_background=True` 时只重置框选、保留底图 —— 用户常见需求是
    "图是对的，房间框错了，重框一遍"，不该逼他重新上传。
    """
    raw = _read_raw(home_id) or {}
    bg = raw.get("background") if isinstance(raw.get("background"), dict) else None

    if keep_background and bg and background_path(home_id) is not None:
        doc = {
            "version": 1,
            "home_id": home_id,
            "updated_at": dt.datetime.now().replace(microsecond=0).isoformat(),
            "view_box": {"x": 0, "y": 0, "w": int(bg["width"]), "h": int(bg["height"])},
            "areas": [],
            "background": bg,
        }
        plan_path(home_id).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("套房 %s 的框选已重置（保留底图）", home_id)
        return load_floorplan(db, home_id)

    plan_path(home_id).unlink(missing_ok=True)
    bg_file = asset_dir(home_id) / "base.png"
    bg_file.unlink(missing_ok=True)
    log.info("套房 %s 的户型已恢复默认（含删除底图）", home_id)
    return load_floorplan(db, home_id)
