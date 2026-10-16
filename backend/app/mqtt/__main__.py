"""独立运行 MQTT 落库服务：

    cd backend
    python -m app.mqtt                    # 一直跑，Ctrl+C 停
    python -m app.mqtt --duration 120     # 跑 2 分钟后自动退出（便于验证）
    python -m app.mqtt --verbose          # 打开 INFO 日志
    python -m app.mqtt --status-every 30  # 每 30 秒打一次统计

先启动 broker（gateway/start_mqtt.cmd）与生成器，再看数据有没有进库：

    python -m gateway.simulator.publisher --speed 60 --duration 1440
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m app.mqtt",
        description="MQTT → MySQL 落库服务（只消费观测数据，不产生控制指令）",
    )
    ap.add_argument("--topic", default="smart_home/#", help="订阅主题（默认 smart_home/#）")
    ap.add_argument("--qos", type=int, default=1, help="订阅 QoS（默认 1）")
    ap.add_argument("--duration", type=float, default=0.0, help="运行多少秒后退出，0=一直跑")
    ap.add_argument("--flush-interval", type=float, default=1.0, help="批量落库间隔（秒）")
    ap.add_argument("--status-every", type=float, default=30.0, help="每多少秒打印一次统计")
    ap.add_argument("--verbose", action="store_true", help="打印 INFO 级日志")
    return ap.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    from app.mqtt.subscriber import MqttIngestService

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    service = MqttIngestService(topic=args.topic, qos=args.qos, flush_interval=args.flush_interval)
    ing = service.ingestor
    print("=" * 78)
    print("MQTT 落库服务")
    print("=" * 78)
    print(f"  broker      : {service.settings.mqtt_host}:{service.settings.mqtt_port}")
    print(f"  订阅主题    : {args.topic} (QoS {args.qos})")
    print(f"  设备目录    : {len(ing.catalog)} 台")
    print(f"  电价时段    : {ing.tariff.describe() if ing.tariff else '未加载'}")
    print(f"  落库间隔    : {args.flush_interval:.1f} 秒（批量提交）")
    print(f"  运行时长    : {args.duration:.0f} 秒" if args.duration else "  运行时长    : 不限（Ctrl+C 停）")
    print("=" * 78)
    print()

    runner = asyncio.create_task(service.run())
    status = (
        asyncio.create_task(_status_loop(service, args.status_every))
        if args.status_every > 0
        else None
    )

    if args.duration > 0:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(runner), timeout=args.duration)
        service.request_stop()
    else:
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        service.request_stop()

    runner.cancel()
    if status:
        status.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
    if status:
        with contextlib.suppress(asyncio.CancelledError):
            await status

    await service.flush_now()
    st = service.stats()
    print()
    print("=" * 78)
    print("落库统计")
    print("=" * 78)
    print(f"  会话数 / 重连数 : {st['service']['sessions']} / {st['service']['reconnects']}")
    print(f"  收到消息        : {st['ingest']['received']}")
    print(f"  拒收（不合格）  : {st['ingest']['rejected']}   未知设备: {st['ingest']['unknown_device']}")
    print(f"  重复 seq 丢弃   : {st['ingest']['duplicates']}")
    print(f"  retained 快照   : {st['ingest']['retained_snapshots']}（broker 回放的历史状态，不参与时间基准）")
    print(f"  入库指标行      : {st['ingest']['readings']}")
    print(f"  入库能耗行      : {st['ingest']['energies']}")
    print(f"  入库事件行      : {st['ingest']['events']}（去重跳过 {st['ingest']['event_deduped']}）")
    print(f"  质量标记        : stale {st['ingest']['stale']} · out_of_range {st['ingest']['out_of_range']}")
    print(f"  刷新次数        : {st['ingest']['flushes']}    错误: {st['ingest']['errors']}")
    print(f"  缓存设备数      : {st['cached_devices']}")
    if st["ingest"]["last_error"]:
        print(f"  最后错误        : {st['ingest']['last_error']}")
    return 0


async def _status_loop(service, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        st = service.stats()
        i, s = st["ingest"], st["service"]
        print(
            f"  [状态] 连接={'✅' if s['connected'] else '❌'} 收到 {i['received']} | "
            f"指标 {i['readings']} 能耗 {i['energies']} 事件 {i['events']} | "
            f"丢弃 重复 {i['duplicates']} 未知 {i['unknown_device']} 不合格 {i['rejected']} | "
            f"stale {i['stale']} | 缓存 {st['cached_devices']} 台"
        )


def main(argv: list[str] | None = None) -> int:
    # ⚠️ 必须在 asyncio.run() 之前调用：Windows 默认的 ProactorEventLoop
    # 不支持 aiomqtt（paho 的 asyncio 封装）依赖的 add_writer/remove_writer，
    # 症状是"连不上 broker 但报错像无关噪声"。详见 app/core/asyncio_compat.py
    from app.core.asyncio_compat import ensure_selector_loop_on_windows

    if ensure_selector_loop_on_windows():
        print("[loop] Windows 事件循环已切换为 Selector（aiomqtt 需要）")

    args = parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\n收到中断，已停止。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
