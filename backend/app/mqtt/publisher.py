"""向设备下发控制指令（"后端 → 设备"方向）。

⚠️ 为什么这里用 paho，而订阅链路用 aiomqtt
----------------------------------------
| | 订阅（`subscriber.py`） | 发布（本模块） |
|---|---|---|
| 客户端 | aiomqtt（原生 async） | paho（同步） |
| 运行位置 | 独立线程的常驻事件循环 | `def` 端点 / 线程池里的短操作 |
| 原因 | 长驻、要与 FastAPI 解耦 | 短操作，不值得为此造一次性事件循环 |

选型里说的"**后端禁用 paho**"针对的是"把 paho 用在 async 事件循环里"——
那会阻塞整个后端。本模块只在**同步上下文**被调用，所以是安全的。

🚨 **这条边界必须守住：不要在 `async def` 里直接调用本模块。**
   需要的话走 `await asyncio.to_thread(publish_command, ...)`。

⚠️ 本模块**不判断风险**
--------------------
它不检查设备是否高风险。"该不该发"的决策集中在 `services/scenes/executor.py` 一处，
不散落在每个发送点上 —— 门锁 / 燃气阀 / 总闸的指令**根本不会走到这里**。
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
from typing import Any

from app.config.settings import get_settings

log = logging.getLogger("app.mqtt.publisher")


@functools.lru_cache(maxsize=1)
def _client():
    """paho 客户端（进程内单例，`loop_start()` 起了自己的后台线程）。

    `lru_cache` 不会缓存异常 —— 所以 broker 没起时，每次调用都会重试连接，
    恢复后自然就通了，不需要手工重启服务。
    """
    import paho.mqtt.client as mqtt

    s = get_settings()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{s.mqtt_client_id_prefix}_cmd",
    )
    if s.mqtt_username:
        client.username_pw_set(s.mqtt_username, s.mqtt_password or None)
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.connect(s.mqtt_host, s.mqtt_port, keepalive=30)
    client.loop_start()
    log.info("指令发布客户端已连接 %s:%s", s.mqtt_host, s.mqtt_port)
    return client


def build_topic(home_id: int, room_code: str, device_code: str) -> str:
    """与统一契约保持一致：`smart_home/{home_id}/room/{room_code}/device/{device_code}/cmd`"""
    return f"smart_home/{home_id}/room/{room_code}/device/{device_code}/cmd"


def build_payload(
    *,
    device_code: str,
    room_code: str,
    capability: str,
    value: Any,
    command_id: int | None = None,
    issued_by: str | None = None,
    issued_at: str | None = None,
) -> str:
    body = {
        "device_code": device_code,
        "room_code": room_code,
        "kind": "cmd",
        "issued_at": issued_at,
        "command_id": command_id,
        "issued_by": issued_by,
        "source": "backend",
        "metrics": {capability: value},
        "units": {},
    }
    return json.dumps(body, ensure_ascii=False)


@dataclasses.dataclass
class PublishResult:
    topic: str
    payload: str
    mid: int | None = None
    ok: bool = True
    error: str | None = None


def publish_command(
    *,
    home_id: int,
    room_code: str,
    device_code: str,
    capability: str,
    value: Any,
    command_id: int | None = None,
    issued_by: str | None = None,
    qos: int = 1,
) -> PublishResult:
    """下发一条控制指令。失败时抛异常，由调用方决定重试与降级。"""
    import datetime as dt

    topic = build_topic(home_id, room_code, device_code)
    payload = build_payload(
        device_code=device_code,
        room_code=room_code,
        capability=capability,
        value=value,
        command_id=command_id,
        issued_by=issued_by,
        issued_at=dt.datetime.now().isoformat(timespec="seconds"),
    )

    client = _client()
    info = client.publish(topic, payload, qos=qos, retain=False)
    # wait_for_publish 会等 PUBACK（QoS1）；超时说明网络或 broker 有问题
    info.wait_for_publish(timeout=5)
    if not info.is_published():
        raise TimeoutError(f"指令未在 5 秒内完成投递（topic={topic}）")

    log.info("已下发指令 #%s → %s.%s = %r", command_id, device_code, capability, value)
    return PublishResult(topic=topic, payload=payload, mid=getattr(info, "mid", None))


def is_connected() -> bool:
    try:
        return bool(_client().is_connected())
    except Exception:  # noqa: BLE001
        return False


def shutdown() -> None:
    try:
        client = _client()
        client.loop_stop()
        client.disconnect()
    except Exception:  # noqa: BLE001
        pass
    _client.cache_clear()
