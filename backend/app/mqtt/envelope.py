"""统一消息信封：topic 与 payload 的解析、契约校验、时区规范化。

契约（已冻结）
--------------
topic:
    smart_home/{home_id}/room/{room_code}/device/{device_code}/{kind}
    kind ∈ state | telemetry | event | cmd | cmd_ack

payload:
    {
      "device_code": "living_room_light_main",
      "room_code": "living_room",          # 冗余但便于排障
      "kind": "state",
      "observed_at": "2026-10-15T21:03:00.000+08:00",
      "seq": 128,
      "source": "simulator",              # simulator / mqtt / csv / real_gateway
      "quality": "good",                  # good / suspect / bad
      "metrics": {"power_on": true, "brightness": 60},
      "units": {"brightness": "%"}
    }
事件消息额外带 `event_code` / `level` / `title` / `detail` / `suggested_action`。

三条校验，都是"宁可拒收，也不入库错数据"
------------------------------------
1. **topic 必须匹配契约** —— 不匹配说明发布方拼错了，放进去就是垃圾数据。
2. **payload 里的 device_code 必须与 topic 里的一致** ——
   不一致意味着数据会被写到另一台设备名下（张冠李戴），比丢数据严重得多。
3. **observed_at 必须存在** —— 绝不用"当前时间"顶上：那样补发的旧数据
   会被伪装成实时数据，直接破坏"过期不得伪装实时"这条硬要求。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: topic 契约正则。device/room code 只允许字母数字下划线中划线（避免中文编码问题）。
TOPIC_PATTERN = re.compile(
    r"^smart_home/(?P<home_id>\d+)"
    r"/room/(?P<room_code>[A-Za-z0-9_\-]+)"
    r"/device/(?P<device_code>[A-Za-z0-9_\-]+)"
    r"/(?P<kind>[a-z_]+)$"
)

READING_KINDS = ("state", "telemetry")
EVENT_KIND = "event"
CONTROL_KINDS = ("cmd", "cmd_ack")

#: 质量标记的取值范围。非法值一律保守降级为 suspect，而不是当成 good。
VALID_QUALITY = ("good", "suspect", "bad")


class EnvelopeError(ValueError):
    """信封不合格：topic 不匹配 / JSON 坏了 / 关键字段缺失。"""


class TopicRef(BaseModel):
    """从 topic 解析出来的定位信息。"""

    model_config = ConfigDict(frozen=True)

    home_id: int
    room_code: str
    device_code: str
    kind: str
    raw: str


class Envelope(BaseModel):
    """统一消息信封。"""

    # extra="allow"：事件消息把 title/detail/suggested_action 展开在顶层，
    # 保留它们便于原样存档到 sensor_events.payload。
    model_config = ConfigDict(extra="allow")

    device_code: str
    kind: str
    observed_at: dt.datetime
    room_code: str | None = None
    seq: int | None = None
    source: str = "unknown"
    quality: str = "good"
    metrics: dict[str, Any] = Field(default_factory=dict)
    units: dict[str, str] = Field(default_factory=dict)

    #: 生成器标注"这条是【故意造】的坏数据"，便于排障时区分真假异常
    fault_injected: str | None = None

    # ---------------- 事件消息字段 ----------------
    event_code: str | None = None
    level: str | None = None
    title: str | None = None
    detail: str | None = None
    suggested_action: str | None = None

    @field_validator("metrics", mode="before")
    @classmethod
    def _metrics_must_be_mapping(cls, v: Any) -> Any:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("metrics 必须是对象（metric → 值）")
        return v

    @field_validator("units", mode="before")
    @classmethod
    def _units_must_be_mapping(cls, v: Any) -> Any:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("units 必须是对象")
        return {str(k): str(val) for k, val in v.items()}

    @field_validator("quality", mode="before")
    @classmethod
    def _normalize_quality(cls, v: Any) -> str:
        q = str(v or "good").strip().lower()
        return q if q in VALID_QUALITY else "suspect"

    @property
    def is_event(self) -> bool:
        return self.kind == EVENT_KIND

    @property
    def is_control(self) -> bool:
        return self.kind in CONTROL_KINDS


def parse_topic(topic: str) -> TopicRef:
    m = TOPIC_PATTERN.match(topic.strip())
    if not m:
        raise EnvelopeError(f"topic 不符合契约: {topic!r}")
    return TopicRef(**m.groupdict(), raw=topic)


def parse_payload(raw: bytes | str) -> Envelope:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise EnvelopeError(f"payload 不是合法 UTF-8: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise EnvelopeError(f"payload 不是合法 JSON: {e}") from e
    if not isinstance(data, dict):
        raise EnvelopeError("payload 顶层必须是 JSON 对象")
    try:
        return Envelope(**data)
    except ValidationError as e:
        raise EnvelopeError(f"信封字段校验失败: {e.errors()[:2]}") from e


def parse(topic: str, payload: bytes | str) -> tuple[TopicRef, Envelope]:
    """解析并交叉校验 topic 与 payload。"""
    ref = parse_topic(topic)
    env = parse_payload(payload)
    if env.device_code != ref.device_code:
        raise EnvelopeError(
            f"topic 的 device_code={ref.device_code!r} 与 payload 的 "
            f"{env.device_code!r} 不一致（会导致数据写到别的设备名下）"
        )
    return ref, env


def to_local_naive(moment: dt.datetime, tz: dt.tzinfo) -> dt.datetime:
    """转成本地时区的 naive datetime（入库用）。

    MySQL 的 DATETIME 不带时区，必须选一种口径统一存。这里选**本地时间**：
    分时电价（峰谷时段）与人的作息节律都是按本地时间定义的，
    存本地时间可以避免在每个查询/聚合处都做一次时区换算（那是最容易出错的地方）。
    代价是跨时区部署时要改——本项目单时区（Asia/Shanghai），可控。
    """
    if moment.tzinfo is None:
        # 没带时区 → 视为已经是本地时间（不做假设性的偏移）
        return moment
    return moment.astimezone(tz).replace(tzinfo=None)
