"""工具层的共享小工具（非业务）。

为什么单独一个模块而不是放 `tools/__init__.py`
--------------------------------------------
`__init__.py` 要导入各个工具模块来注册白名单，而工具模块又需要这里的函数 ——
放一起会形成循环导入。独立成 `_common` 就没了这个问题。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal


def resolve_anchor(ctx: Any) -> dt.datetime:
    """确定"数据最新时间"作为查询锚点。

    三级取，越靠前越准：
    1. **缓存里的最新实时值时间** —— 最贴近"此刻"；
    2. **库里遥测表的最大时间** —— 进程刚起、缓存还没预热时用这个。
       ⚠️ 少了这一级就会出现"库里明明有数据，工具却说查不到"的假空结果，
       然后模型会顺理成章地告诉用户"没有数据"；
    3. **本机当前时间** —— 库里也没数据时的兜底。

    为什么不用 `datetime.now()` 直接当锚点：模拟器按倍速跑，数据时间戳会跑到
    真实时钟之外。真实部署时这两者等价，所以这个选择没有副作用。
    """
    from app.services import overview

    anchor = overview.data_anchor(ctx.catalog, ctx.cache)
    if anchor is not None:
        return anchor

    with SessionLocal() as db:
        row = db.execute(
            text("SELECT MAX(observed_at) FROM device_readings")
        ).scalar()
    if row is not None:
        return row

    return dt.datetime.now()


def resolve_window(ctx: Any, hours: int, *, cap_hours: int = 24 * 31) -> tuple[dt.datetime, dt.datetime]:
    """按锚点算出一个查询窗口 `(from, to)`。"""
    try:
        span = max(1, min(int(hours or 24), cap_hours))
    except (TypeError, ValueError):
        span = 24
    to_dt = resolve_anchor(ctx)
    return to_dt - dt.timedelta(hours=span), to_dt
