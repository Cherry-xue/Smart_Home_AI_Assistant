"""FastAPI 入口。

启动顺序（lifespan）
--------------------
1. **加载设备目录** —— 一次查库，之后全走内存（设备/房间/能力档案）；
2. **预热最新值缓存** —— 让"服务刚起来"就能回答"客厅现在多少度"，
   而不是干等到第一条 MQTT 消息到达（重启后态势立刻可见）；
3. **按 `DATA_MODE` 决定是否拉起 MQTT 订阅线程** ——
   只有 `mqtt` 才连 broker；`mock` / `csv` 下接口照常可用（数据源可插拔的落地）。

⚠️ Windows 事件循环：这里没有把订阅服务放进 FastAPI 的循环
--------------------------------------------------------
aiomqtt 要求 SelectorEventLoop，而 uvicorn 在 Windows 上用的是 ProactorEventLoop
（`--reload` 时更是**显式设置**的），而"事件循环一旦创建就不能改类型"。

所以订阅服务跑在**独立线程**里（`mqtt/service_worker.py`）：线程自带循环，
与 uvicorn 用什么循环完全无关，`--reload` 也能用；同时仍在同一进程内，
**共享同一份设备目录与最新值缓存**，所以 API 能直接读到实时值。

关于 CORS
---------
**默认不启用**（`CORS_ORIGINS` 留空）：Web 端由 Vite 的 proxy 转发 `/api`，
同源请求不需要 CORS。

但**手机端接入后这条就不够了** —— H5 版跑在浏览器内核里会带 Origin，
直连 `http://<局域网IP>:8000` 时没有 CORS 响应头就会被浏览器拦掉。
所以现在是"按需启用 + 来源白名单"（`CORS_ORIGINS` 逗号分隔），
而不是无脑 `allow_origins=["*"]`：那等于让任何网页都能调这个家里的接口。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ⚠️ 必须在任何事件循环创建之前执行（uvicorn import 应用是在建循环之前）
from app.core.asyncio_compat import ensure_selector_loop_on_windows

ensure_selector_loop_on_windows()

from app.api import api_router  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.core.tariff import get_tariff  # noqa: E402
from app.db.catalog import DeviceCatalog  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.mqtt.cache import LatestValueCache  # noqa: E402
from app.mqtt.service_worker import MqttWorker  # noqa: E402
from app.services.analytics import QueryError  # noqa: E402
from app.services.preload import preload_latest_values  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("app.main")

API_DESCRIPTION = """\
智能家居 AI 助手的后端 API（课程项目 · 本地直跑）。

### ⚠️ 关于数据来源
本项目**没有真实设备采集**：所有数据来自**数据生成器**（`gateway/simulator`，经 MQTT 上报）
或 **MQTT 客户端手工注入**。每条记录都带 `source` 与 `quality` 元数据，
以区分"模拟"与"真实"——**模拟数据不得伪装成实测值**。

### 🔒 安全边界
门锁、燃气阀、电气总闸、布防撤防属于**高风险**设备：
接口层只提供"查询"与"生成待确认建议"，**不存在任何直接执行通道**
（详情里 `auto_exec_allowed` 恒为 false）。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("启动中… 环境=%s 数据源=%s 时区=%s", settings.app_env, settings.data_mode, settings.timezone)

    # ---------------- 1) 设备目录 ----------------
    catalog = await asyncio.to_thread(DeviceCatalog.load, engine)
    app.state.catalog = catalog
    log.info("设备目录已加载：%d 台设备 / %d 个房间", len(catalog), len(catalog.rooms))

    # ---------------- 2) 缓存 + 预热 ----------------
    cache = LatestValueCache()
    app.state.cache = cache
    app.state.cache_preloaded = 0
    try:
        stats = await asyncio.to_thread(preload_latest_values, engine, catalog, cache)
        app.state.cache_preloaded = stats["values_filled"]
        log.info(
            "缓存预热完成：扫描 %d 行 → 覆盖 %d 台设备 / 填入 %d 个值（最近 2 天）",
            stats["rows_scanned"], stats["devices"], stats["values_filled"],
        )
    except Exception:  # noqa: BLE001
        # 预热失败不该拦住服务启动 —— 实时数据到达后会自然填充
        log.exception("缓存预热失败（服务继续启动；实时数据到达后会自然填充）")

    # ---------------- 3) MQTT 订阅线程 ----------------
    app.state.mqtt_service = None
    app.state.mqtt_worker = None
    if settings.data_mode == "mqtt":
        from app.mqtt.ingest import Ingestor
        from app.mqtt.subscriber import MqttIngestService

        # ⚠️ 把 app.state.cache 传进去：订阅线程与 API 端点必须共用同一个缓存实例，
        # 否则会变成"一边写、一边读"，接口永远读不到实时值（详见 Ingestor.__init__）
        ingestor = Ingestor(catalog, settings.tzinfo, get_tariff(), cache=cache)
        service = MqttIngestService(settings=settings, ingestor=ingestor)
        worker = MqttWorker(service)
        worker.start()
        app.state.mqtt_service = service
        app.state.mqtt_worker = worker
        log.info(
            "MQTT 订阅线程已启动 → %s:%s（独立线程，自带事件循环）",
            settings.mqtt_host, settings.mqtt_port,
        )
    else:
        log.info("DATA_MODE=%s → 不启动 MQTT 订阅（接口照常可用）", settings.data_mode)

    log.info("服务就绪 → http://%s:%s/docs", settings.api_host, settings.api_port)
    try:
        yield
    finally:
        worker: MqttWorker | None = getattr(app.state, "mqtt_worker", None)
        if worker is not None:
            log.info("正在停止 MQTT 订阅线程…")
            stopped = await asyncio.to_thread(worker.stop)
            log.info("MQTT 订阅线程%s", "已退出" if stopped else "未在超时内退出（daemon，随进程结束）")
        log.info("服务已停止")


app = FastAPI(
    title="智能家居 AI 助手 · API",
    version="0.2.0",
    description=API_DESCRIPTION,
    lifespan=lifespan,
)

# ---------------- CORS（手机 H5 直连时才需要）----------------
# 默认关闭：Web 端走 Vite proxy 同源转发，本来就不需要 CORS。
# 但手机上的 H5 版跑在浏览器内核里，会带 Origin 直连后端 ——
# 此时必须把来源列进 `CORS_ORIGINS`，否则浏览器会在预检阶段就拦掉，
# 现象是"请求像是没发出去"，而不是后端报错。
# ⚠️ 不要图省事填 `*`：那等于让任何网页都能调这个家里的接口。
_cors_origins = get_settings().cors_origin_list
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    log.info("已启用 CORS，允许来源：%s", _cors_origins)


@app.exception_handler(QueryError)
async def _query_error_handler(request: Request, exc: QueryError) -> JSONResponse:
    """把查询参数类错误统一转成 400（比如时间跨度过大、from 晚于 to）。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(api_router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "智能家居 AI 助手 · API",
        "docs": "/docs",
        "health": "/api/health",
        "note": "本项目的设备数据来自模拟器 / MQTT 手工注入，没有真实设备采集",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
