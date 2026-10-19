"""户型图几何的 API 模型。"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class ViewBoxIn(BaseModel):
    x: float = 0
    y: float = 0
    w: float
    h: float


class AreaIn(BaseModel):
    code: str = Field(..., description="房间编码；除交通空间外必须是 rooms 表里已有的 code")
    name: str | None = Field(
        None,
        description="显示名。真实房间传了就**写回 rooms.name**（全站唯一真相）；"
        "交通空间必须给名字。除走廊外，同一套房里房间名必须唯一",
    )
    type: str | None = Field(
        None,
        max_length=24,
        description="房间类型（客厅/卧室/卫生间/走廊…，可自由填）。"
        "只用于展示与默认命名，不参与唯一约束；走廊类型 = 交通空间",
    )
    x: float
    y: float
    w: float
    h: float
    is_circulation: bool = Field(
        False, description="交通空间（走廊/通道）：不挂设备、不可点选"
    )


class FloorplanIn(BaseModel):
    view_box: ViewBoxIn
    areas: list[AreaIn]


class AreaOut(BaseModel):
    code: str
    name: str = Field(..., description="显示名（房间以数据库为准）")
    type: str = Field("", description="房间类型（自由文本；走廊表示交通空间）")
    x: float
    y: float
    w: float
    h: float
    is_circulation: bool
    known_room: bool = Field(..., description="false 表示这是交通空间，rooms 表里没有它")


class BackgroundOut(BaseModel):
    url: str = Field(..., description="底图地址，已带版本号，可直接给 <image> 用")
    width: int
    height: int
    opacity: float = 0.9


class MissingRoomOut(BaseModel):
    code: str
    name: str
    device_count: int


class FloorplanOut(BaseModel):
    home_id: int = 1
    source: str = Field(..., description="default（内置）/ custom（自定义过）/ empty（还没画）")
    is_custom: bool
    updated_at: dt.datetime | None = None
    view_box: ViewBoxIn
    areas: list[AreaOut] = Field(default_factory=list)
    background: BackgroundOut | None = None
    missing_rooms: list[MissingRoomOut] = Field(
        default_factory=list, description="库里有、但没画到图上的房间（它们的设备不会显示）"
    )


class FloorplanSaveOut(BaseModel):
    message: str = ""
    floorplan: FloorplanOut
