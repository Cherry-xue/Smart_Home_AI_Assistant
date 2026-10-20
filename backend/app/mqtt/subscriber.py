"""aiomqtt 订阅服务：把 broker 上的观测数据持续灌进数据库。

为什么用 aiomqtt（而不是 paho）
------------------------------
后端是 async 的。paho 的 `publish()` / `loop()` 是阻塞调用，
直接用在 `async def` 里会卡住整个事件循环 —— 表现为"LLM 流式输出突然卡顿"，
而且这种卡顿很难归因。aiomqtt 是原生 async 客户端，与 FastAPI 天然共处。

**生成器（独立进程）用 paho 无所谓**，那边没有事件循环要照顾。

两个容易被忽略的工程点
----------------------
1. **落库不能直接 await**：SQLAlchemy 是同步的，写远程库一次往返不便宜。
   直接调用 = 阻塞事件循环 = 消息积压。所以统一丢 `asyncio.to_thread()`。
2. **必须批量提交**：32 台设备每 5 秒一条，逐条 commit 会让网络往返成为瓶颈。
   这里按"缓冲换出 + 批量 executemany"处理，一次事务写几百行。

断线重连
--------
MQTT 是长连接，broker 重启/网络抖动都会断。这里用指数退避重连（1s → 30s 封顶），
并且**断线期间不丢缓冲** —— 已经解析好的行仍然会被 flush 写库。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import logging
from typing import Any

import aiomqtt

from app.config.settings import Settings, get_settings
from app.core.tariff import get_tariff
from app.db.catalog import DeviceCatalog
from app.db.session import engine
from app.mqtt.ingest import Ingestor, PendingBatch

log = logging.getLogger("app.mqtt.subscriber")

DEFAULT_TOPIC = "smart_home/#"


@dataclasses.dataclass
class ServiceStats:
    connected: bool = False
    reconnects: int = 0
    sessions: int = 0
    messages_seen: int = 0
    last_message_at: dt.datetime | None = None
    connected_since: dt.datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class MqttIngestService:
    """订阅 → 解析 → 落库的常驻服务。

    用法（进程内）：
        service = MqttIngestService()
        await service.run()          # 阻塞直到 request_stop()

    或独立进程：
        python -m app.mqtt --duration 120
    """

    def __init__(
        self,
        *,
        topic: str = DEFAULT_TOPIC,
        qos: int = 1,
        flush_interval: float = 1.0,
        settings: Settings | None = None,
        ingestor: Ingestor | None = None,
    ):
        self.settings = settings or get_settings()
        self.topic = topic
        self.qos = qos
        self.flush_interval = flush_interval
        #: 重连退避（秒）。**每次成功连上都会复位成 1.0** —— 见 `_session`。
        self._backoff = 1.0

        if ingestor is None:
            catalog = DeviceCatalog.load(engine)
            ingestor = Ingestor(catalog, self.settings.tzinfo, get_tariff())
            log.info("设备目录已加载：%d 台设备", len(catalog))
        self.ingestor = ingestor

        self.service_stats = ServiceStats()
        self._stop = asyncio.Event()

    # ================================================================== 生命周期
    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """常驻运行，直到 `request_stop()` 或任务被取消。"""
        while not self._stop.is_set():
            try:
                await self._session()
            except aiomqtt.MqttError as e:
                self.service_stats.connected = False
                self.service_stats.reconnects += 1
                log.warning("MQTT 断开（%s），%.0f 秒后重连", e, self._backoff)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._backoff)
                self._backoff = min(self._backoff * 2, 30.0)
            except asyncio.CancelledError:
                self._stop.set()
                await self._flush()
                raise

    async def _session(self) -> None:
        client_kwargs: dict[str, Any] = {
            "hostname": self.settings.mqtt_host,
            "port": self.settings.mqtt_port,
            "identifier": f"{self.settings.mqtt_client_id_prefix}_ingest",
        }
        if self.settings.mqtt_username:
            client_kwargs["username"] = self.settings.mqtt_username
            client_kwargs["password"] = self.settings.mqtt_password or None

        async with aiomqtt.Client(**client_kwargs) as client:
            await client.subscribe(self.topic, qos=self.qos)
            self.service_stats.connected = True
            self.service_stats.connected_since = dt.datetime.now()
            self.service_stats.sessions += 1
            # ⚠️ 连上就把退避复位。原先是只在"会话正常结束"时才复位，而断线是抛异常的，
            #    于是退避一路累积：1→2→4→8→16→30s —— 每断一次，下一次恢复就更慢
            #    （实测第二次断线要等 16 秒、第三次 30 秒）。
            self._backoff = 1.0
            log.info("已连接 %s:%s，订阅 %s", self.settings.mqtt_host, self.settings.mqtt_port, self.topic)

            flusher = asyncio.create_task(self._flush_loop())
            try:
                async for message in client.messages:
                    if self._stop.is_set():
                        break
                    self._on_message(message)
            finally:
                flusher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await flusher
                await self._flush()
                self.service_stats.connected = False
                log.info("MQTT 会话结束，缓冲已清空")

    # ================================================================== 内部
    def _on_message(self, message: aiomqtt.Message) -> None:
        self.service_stats.messages_seen += 1
        self.service_stats.last_message_at = dt.datetime.now()
        topic = str(message.topic)

        # ⚠️ `smart_home/#` 会把**控制回执**（cmd_ack）也收进来。
        # 它不是观测数据，丢给 Ingestor 只会报"未知消息类型"，
        # 必须在这里分流到回执处理 —— 否则后端永远不知道设备到底执行了没有。
        if topic.endswith("/cmd_ack"):
            try:
                self._handle_cmd_ack(message.payload)
            except Exception:  # noqa: BLE001
                log.exception("处理控制回执失败 topic=%s", topic)
            return

        try:
            # retain 标志要一路传下去：它决定这条消息是不是"broker 回放的历史快照"，
            # 进而决定要不要参与"时间倒退"判定（见 quality.py 的说明）
            self.ingestor.handle(
                topic,
                message.payload,
                retain=bool(getattr(message, "retain", False)),
            )
        except Exception:  # noqa: BLE001
            # 一条坏消息绝不能把订阅循环打挂
            self.ingestor.stats.errors += 1
            log.exception("处理消息失败 topic=%s", topic)

    def _handle_cmd_ack(self, payload: bytes) -> None:
        """处理设备回执：把"设备到底执行了没有"写回指令记录。

        为什么必须有这一步：下发成功只说明**报文发出去了**，不代表设备状态变了
        （虚拟设备运行时没启动时，MQTT 上根本没人接）。
        没有回执，上层就只能写死一句"已完成" —— 那就是谎报。

        回执落到 `device_commands.result_message`，形式：
            `[回执] 已确认：power_on=true` / `[回执] 设备拒绝：xxx`
        """
        import json

        from sqlalchemy import text

        from app.db.session import SessionLocal

        data = json.loads(payload.decode("utf-8"))
        command_id = data.get("command_id")
        if not command_id:
            return

        accepted = bool(data.get("accepted"))
        results = data.get("results") or []
        parts = []
        for r in results:
            mark = "✅" if r.get("accepted") else "❌"
            # 用 JSON 形式打印值：Python 的 `False` / `True` 混进中文回执里很刺眼
            val = json.dumps(r.get("value"), ensure_ascii=False)
            parts.append(f"{mark}{r.get('capability')}={val}（{r.get('note','')}）")
        detail = "；".join(parts) or ("设备已确认" if accepted else "设备拒绝")
        message = f"[回执] {'设备已确认' if accepted else '设备拒绝'}：{detail}"

        db = SessionLocal()
        try:
            # ⚠️ 设备明确拒绝时必须把状态改成 failed。
            #    否则断电/无供电时指令记录仍是 executed，统计上等于"执行成功"，
            #    是另一种形式的谎报（`result_message` 说拒绝、`status` 说成功）。
            db.execute(
                text(
                    """
                    UPDATE device_commands
                    SET result_message = :msg,
                        executed_at = COALESCE(executed_at, :now),
                        status = CASE WHEN :ok = 1 THEN status ELSE 'failed' END
                    WHERE id = :cid
                    """
                ),
                {
                    "msg": message[:500],
                    "now": dt.datetime.now(),
                    "cid": command_id,
                    "ok": 1 if accepted else 0,
                },
            )
            db.commit()
            log.info("收到设备回执 command_id=%s → %s", command_id,
                     "已确认" if accepted else "被拒绝")
        finally:
            db.close()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            await self._flush()

    async def _flush(self) -> int:
        batch: PendingBatch = self.ingestor.take_batch()
        if batch.is_empty:
            return 0
        try:
            written = await asyncio.to_thread(self.ingestor.write_batch, batch)
        except Exception:  # noqa: BLE001
            # write_batch 里已经 rollback + 记错误；这里只保证循环继续
            log.error("本批 %d 行写入失败，已丢弃（错误已记入统计）", len(batch))
            return 0
        self.ingestor.on_flushed(written)
        return written

    async def flush_now(self) -> int:
        """手动触发一次落库（供测试与关停时使用）。"""
        return await self._flush()

    # ================================================================== 状态
    def stats(self) -> dict[str, Any]:
        return {
            "service": self.service_stats.as_dict(),
            "ingest": self.ingestor.stats.as_dict(),
            "catalog_size": len(self.ingestor.catalog),
            "cached_devices": len(self.ingestor.cache),
        }
