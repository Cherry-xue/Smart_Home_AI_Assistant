"""MQTT 订阅 → 解析 → 落库 链路。

分工
----
| 模块 | 职责 |
|---|---|
| `envelope.py`   | topic / payload 的解析与契约校验（不合格的直接拒收） |
| `quality.py`    | 数据质量判定：去重、时间倒退（补发）、越界标注 |
| `cache.py`      | 最新值缓存（供 API / Agent 查"现在多少度"，不打库） |
| `ingest.py`     | 落库：遥测 → `device_readings`，功率 → `energy_readings`，事件 → `sensor_events` |
| `subscriber.py` | aiomqtt 订阅服务：断线重连、批量落库、统计 |

⚠️ 方向性纪律：本链路**只消费观测数据**。`cmd` / `cmd_ack` 属于
"后端 → 设备"方向，不在这里处理（高风险指令另有专门的待确认通道，
见规划书：门锁/燃气阀/总闸永不自动执行）。
"""
