"""家庭上下文类工具：最近事件 + 长期记忆。

这两类东西都不是"设备实时值"，但回答里经常要用：
- 安防 Agent 要看最近有没有事件；
- 照护 Agent 要看活动节律；
- 任何 Agent 给建议时都可能需要引用用户偏好。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool
from sqlalchemy import text

from app.db.session import SessionLocal
from app.tools._common import resolve_window

_LEVEL_CN = {"info": "提示", "warning": "警告", "critical": "严重"}


def _as_dict(value: Any) -> dict[str, Any]:
    """把可能为字符串的 JSON 列解析成 dict。

    ⚠️ 用 `text()` 写原生 SQL 时，SQLAlchemy **不知道列的类型**，
    所以 MySQL 的 JSON 列会原样返回字符串而不是 dict。
    不处理的话 `payload.get("title")` 会静默拿到空值 —— 事件详情就会整段丢失，
    而且不会报错，很难发现。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def build_context_tools(ctx: Any) -> list[Any]:
    home_id: int = ctx.home_id
    member_name: str | None = getattr(ctx, "member_name", None)

    @tool
    def read_recent_events(hours: int = 24, level: str | None = None, room: str | None = None) -> str:
        """查询**最近的传感器事件与告警**（燃气泄漏、水浸、烟雾、门窗异常、长时间无活动等）。

        什么时候用：用户问"最近有没有异常""家里安全吗""出过什么事"，
        或你需要判断当前环境是否安全时。

        参数：
        - hours 往前查多少小时，默认 24
        - level 只要某个级别：info（提示）/ warning（警告）/ critical（严重）
        - room  只要某个房间（中文名，如「厨房」）

        返回事件的级别、时间、来源设备与详情。**没有事件就如实说"没有事件"** ——
        "没查到"和"没有发生"在安防语境下都重要，不要含糊。
        """
        from_dt, to_dt = resolve_window(ctx, hours)
        sql = """
            SELECT e.event_code, e.level, e.started_at, e.ended_at, e.payload,
                   d.name AS device_name, d.code AS device_code, r.name AS room_name
            FROM sensor_events e
            JOIN devices d ON d.id = e.device_id
            LEFT JOIN rooms r ON r.id = d.room_id
            WHERE e.started_at >= :f AND e.started_at <= :t
        """
        params: dict[str, Any] = {"f": from_dt, "t": to_dt}
        if level:
            sql += " AND e.level = :lv"
            params["lv"] = level.strip().lower()
        sql += " ORDER BY e.started_at DESC LIMIT 50"

        with SessionLocal() as db:
            try:
                rows = db.execute(text(sql), params).all()
            except Exception as e:  # noqa: BLE001
                return f"查询事件失败：{type(e).__name__}: {e}"

        if room:
            rows = [r for r in rows if room.strip() in (r.room_name or "")]

        if not rows:
            scope = f"（级别 {level}）" if level else ""
            room_scope = f"（房间 {room}）" if room else ""
            return (
                f"最近 {hours} 小时（{from_dt:%m-%d %H:%M} ~ {to_dt:%m-%d %H:%M}）"
                f"{scope}{room_scope}没有记录到任何传感器事件。"
                "请如实告知用户，**不要据此推断「一切正常」** —— 只说明「这段时间没有记录」。"
            )

        lines = [
            f"最近 {hours} 小时共 {len(rows)} 条事件"
            f"（{from_dt:%m-%d %H:%M} ~ {to_dt:%m-%d %H:%M}）："
        ]
        for r in rows:
            lv = _LEVEL_CN.get(r.level, r.level)
            when = r.started_at.strftime("%m-%d %H:%M:%S") if r.started_at else "?"
            payload = _as_dict(r.payload)
            title = payload.get("title") or r.event_code
            lines.append(f"  [{lv}] {when} {r.room_name or ''}·{r.device_name}（{r.device_code}）")
            lines.append(f"       {title}")
            if payload.get("detail"):
                lines.append(f"       详情：{payload['detail']}")
            if payload.get("suggested_action"):
                lines.append(f"       建议动作：{payload['suggested_action']}")
        return "\n".join(lines)

    @tool
    def search_memory(query: str = "", limit: int = 8) -> str:
        """查询**家庭长期记忆与成员偏好**（温度偏好、作息、禁忌、历史反馈等）。

        什么时候用：给建议之前，先看看用户有什么既有偏好；或者用户问"你记得我
        说过什么吗""我一般喜欢多少度"。

        参数：
        - query 检索关键词；**留空则返回全部已沉淀的偏好与记忆**（数量少时更可靠）
        - limit 最多返回几条

        返回每条内容的**来源与置信度**。⚠️ 引用时必须说明这条来自哪里；
        置信度低于 0.5 的不要拿来当结论依据。
        """
        try:
            n = max(1, min(int(limit or 8), 30))
        except (TypeError, ValueError):
            n = 8
        token = (query or "").strip()

        with SessionLocal() as db:
            pref_rows = db.execute(
                text(
                    """
                    SELECT mp.capability_code, mp.value, mp.weight, mp.source,
                           m.nickname AS member_name, m.role_code AS member_role
                    FROM member_preferences mp
                    LEFT JOIN members m ON m.id = mp.member_id
                    WHERE m.home_id = :h
                    ORDER BY mp.weight DESC
                    LIMIT :n
                    """
                ),
                {"h": home_id, "n": n},
            ).all()

            mem_rows = db.execute(
                text(
                    """
                    SELECT id, member_id, memory_type, content, source, confidence, decay_at
                    FROM memories
                    WHERE home_id = :h AND (decay_at IS NULL OR decay_at > NOW())
                    ORDER BY confidence DESC, id DESC
                    LIMIT :n
                    """
                ),
                {"h": home_id, "n": n},
            ).all()

        lines: list[str] = []

        if pref_rows:
            lines.append("【结构化偏好（来自 member_preferences，可直接依据）】")
            for r in pref_rows:
                who = r.member_name or "家庭成员"
                lines.append(
                    f"  · {who}（{r.member_role or '成员'}）：{r.capability_code} = {r.value}"
                    f"（权重 {r.weight}，来源 {r.source}）"
                )

        if mem_rows:
            lines.append("")
            lines.append("【长期记忆（来自 memories，需注明来源与置信度）】")
            for r in mem_rows:
                low = " ⚠️置信度偏低，不宜作为结论依据" if float(r.confidence or 0) < 0.5 else ""
                lines.append(
                    f"  · #{r.id} [{r.memory_type}] {r.content}"
                    f"（来源 {r.source}，置信度 {r.confidence}{low}）"
                )

        if not lines:
            return (
                "目前还没有沉淀任何长期记忆或成员偏好。"
                "请如实告知用户，**不要编造他的习惯**。"
            )

        if token:
            lines.append("")
            lines.append(f"（提示：以上为按 {token!r} 筛选的结果；若都不相关请直接说明）")
        if member_name:
            lines.append(f"（本次发言人：{member_name}）")
        return "\n".join(lines)

    return [read_recent_events, search_memory]
