"""API 依赖：从 `app.state` 取设备目录与最新值缓存。

为什么放 `app.state` 而不是模块级全局
------------------------------------
它们的生命周期由 lifespan 管理（启动时加载 / 关停时释放），
放进 `app.state` 才能做到"一个进程一份 + 测试时可替换"。
模块级全局在测试里无法隔离（多次建 app 会拿到上一条用例的脏数据）。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from app.config.settings import Settings, get_settings
from app.db.catalog import DeviceCatalog
from app.mqtt.cache import LatestValueCache

__all__ = [
    "get_catalog",
    "get_cache",
    "get_settings_dep",
    "get_worker",
    "get_service",
    "get_tool_context",
]


def get_catalog(request: Request) -> DeviceCatalog:
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "设备目录尚未加载完成，请稍后重试"
        )
    return catalog


def get_cache(request: Request) -> LatestValueCache:
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "最新值缓存尚未就绪")
    return cache


def get_worker(request: Request):
    """MQTT 订阅线程（未启用 data_mode=mqtt 时为 None）。"""
    return getattr(request.app.state, "mqtt_worker", None)


def get_service(request: Request):
    return getattr(request.app.state, "mqtt_service", None)


def get_settings_dep() -> Settings:
    return get_settings()


def get_tool_context(
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
) -> "ToolContext":
    """构造 Agent 工具层需要的上下文。

    ⚠️ 用 FastAPI 的依赖注入而不是模块级单例：工具上下文里含着设备目录与缓存，
    测试时可以整体替换掉（否则多次建 app 会串用例）。

    ⚠️ 必须放在 `get_catalog` / `get_cache` **之后**：默认参数里的 `Depends(...)`
    在**函数定义时**就要求名字已存在，顺序颠倒会在 import 阶段抛 NameError
    （表现是"uvicorn 起不来，报一个看不懂的 import_from_string 错误"）。
    """
    from app.tools import ToolContext

    return ToolContext(catalog=catalog, cache=cache, home_id=1)
