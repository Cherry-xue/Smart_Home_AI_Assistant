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
    "mqtt_link_down",
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


def mqtt_link_down(request: Request) -> bool:
    """MQTT 链路是否断开（只对 `DATA_MODE=mqtt` 有意义）。

    为什么"断链"要单独成一个判定
    ----------------------------
    设备在线本来是"最近有上报"（`received_at` 在 600 秒内）。但链路一断，
    后端**收不到任何设备的数据**，它连"设备还在不在"都无从判断 ——
    这时候还显示"32 台在线"就是在撒谎。真实平台遇到网关掉线也是直接把
    下面所有设备标成不可达，而不是等超时。

    ⚠️ `mock` / `csv` 模式本来就不连 broker，不能因此判成"全部离线"。
    """
    settings = get_settings()
    if settings.data_mode != "mqtt":
        return False

    worker = getattr(request.app.state, "mqtt_worker", None)
    if worker is not None:
        try:
            return not bool(worker.status().get("connected", False))
        except Exception:  # noqa: BLE001
            pass

    service = getattr(request.app.state, "mqtt_service", None)
    if service is not None:
        return not bool(service.service_stats.connected)

    # 订阅服务压根没起（启动失败）：这本身就是"链路不可用"
    return True
