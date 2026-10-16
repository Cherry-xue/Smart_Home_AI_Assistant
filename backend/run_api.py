"""启动 API 服务的推荐入口。

用法（在 `backend/` 目录下）：
    python run_api.py                 # 开发模式（热重载）
    python run_api.py --no-reload     # 不带热重载（更稳，适合长时间挂着）
    python run_api.py --port 8001

为什么不直接 `uvicorn app.main:app`
----------------------------------
1. 这里能在**创建事件循环之前**做平台兼容处理，把 Windows 的
   Proactor/Selector 差异挡在启动脚本里（见 `core/asyncio_compat.py`）；
2. 把端口 / 日志级别 / 热重载这些常用参数固化，避免每次手敲；
3. 启动前先做一次**环境自检**（DB 能不能连、broker 配置是什么），
   把"起不来"的原因直接打在控制台上，而不是等某个请求报错才发现。

起服顺序（三个进程）：
    gateway/start_mqtt.cmd                      # 1. broker（每次开机手动起）
    python -m gateway.simulator.publisher --speed 60   # 2. 数据生成器（可选）
    python run_api.py                           # 3. 本服务（含订阅落库线程）
"""

from __future__ import annotations

import argparse
import sys

from app.core.asyncio_compat import ensure_selector_loop_on_windows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from app.config.settings import get_settings

    settings = get_settings()
    ap = argparse.ArgumentParser(description="启动智能家居 AI 助手 API")
    ap.add_argument(
        "--host",
        default=settings.effective_api_host,
        help=(
            f"监听地址（默认 {settings.effective_api_host}）"
            "；要让手机连进来，在 .env 里设 ALLOW_LAN=true（等价于监听 0.0.0.0）"
        ),
    )
    ap.add_argument("--port", type=int, default=settings.api_port, help=f"端口（默认 {settings.api_port}）")
    ap.add_argument("--no-reload", action="store_true", help="关闭热重载（默认开启）")
    ap.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn 日志级别",
    )
    return ap.parse_args(argv)


def preflight() -> None:
    """启动前自检：把关键结论打在控制台上，省得事后猜。"""
    from app.config.settings import get_settings
    from app.core.tariff import get_tariff
    from app.db.session import ping

    settings = get_settings()
    print("=" * 78)
    print("智能家居 AI 助手 · API 启动自检")
    print("=" * 78)
    print(f"  监听地址    : http://{settings.effective_api_host}:{settings.api_port}")
    if settings.effective_api_host == "0.0.0.0":
        print("                （已允许局域网访问 —— 手机端可直连本机 IP）")
    else:
        print("                （仅本机可访问；手机端接入需设 ALLOW_LAN=true）")
    if settings.cors_origin_list:
        print(f"  CORS 允许来源: {settings.cors_origin_list}")
    print(f"  环境 / 数据源: {settings.app_env} / {settings.data_mode}")
    print(f"  业务时区    : {settings.timezone}")
    print(f"  电价时段    : {get_tariff().describe()}")
    print(f"  高风险自动执行: {settings.is_high_risk_auto_exec_enabled}  （必须为 False）")

    try:
        info = ping()
        print(f"  数据库      : ✅ {settings.mysql_host}:{settings.mysql_port}/{settings.mysql_db}"
              f"  MySQL {info['version']} / {info['charset']} / {info['current_user']}")
    except Exception as e:  # noqa: BLE001
        print(f"  数据库      : ❌ {type(e).__name__}: {e}")
        print("                → 检查虚拟机是否开机、.env 里的连接信息是否正确")

    if settings.data_mode == "mqtt":
        print(f"  MQTT broker : {settings.mqtt_host}:{settings.mqtt_port}"
              f"（订阅服务会在独立线程里连接；broker 没起时接口仍可用，但数据不进库）")
    print("=" * 78)
    print()


def main(argv: list[str] | None = None) -> int:
    # 在 uvicorn 创建事件循环之前完成平台兼容处理
    if ensure_selector_loop_on_windows():
        print("[loop] Windows 事件循环已切换为 Selector\n")

    args = parse_args(argv)
    preflight()

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
