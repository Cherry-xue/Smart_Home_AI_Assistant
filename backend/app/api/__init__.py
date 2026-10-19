"""API 路由汇总。统一挂在 `/api` 前缀下（见 `main.py`）。

已实现（M2 态势与 RAG）：
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 系统状态：DB / MQTT / 采集统计 / 缓存 / 离线设备 |
| GET | `/api/homes/{home_id}/rooms` | 房间卡片墙（家庭总览页） |
| GET | `/api/devices` | 设备列表（可按房间/类型过滤），带最新值 |
| GET | `/api/devices/{device_id}` | 单台设备详情 |
| GET | `/api/devices/{device_id}/readings` | 历史读数（画曲线） |
| GET | `/api/devices/{device_id}/capabilities` | 能力档案 |
| GET | `/api/energy/summary` | 能耗账单口径汇总 |
| GET | `/api/knowledge/search` | 语义检索设备说明书 / 安防预案 |
| GET | `/api/knowledge/documents` | 已入库文档清单 |
| GET | `/api/knowledge/status` | 知识库状态 |
| POST | `/api/scenes/preview` | **自然语言 → 动作序列 + 冲突检测**（不执行） |
| POST | `/api/scenes/plan` | 对手动微调过的动作重新编排（不执行） |
| POST | `/api/scenes` | 保存场景 |
| GET | `/api/scenes` | 场景列表 |
| POST | `/api/scenes/{scene_id}/run` | 执行场景（高风险步骤只生成待确认） |
| GET | `/api/scenes/runs/{run_id}` | 执行记录详情 |

已实现（M4 多 Agent 编排）：
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | **多 Agent 对话**（非流式，手机端首期用这个） |
| POST | `/api/chat/stream` | 同上，但按节点 SSE 流式输出（大屏 / H5 用） |
| GET | `/api/confirmations/pending` | 待确认事项列表（手机端弹窗轮询这个） |
| POST | `/api/confirmations/{thread_id}` | 对某条待确认事项给出决定（批准 / 驳回） |

已实现（M6 设备直控）：
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/devices/{device_id}/command` | **给一台设备下发指令**（高风险设备只生成待确认项） |
| GET | `/api/devices/{device_id}/commands` | 指令记录（新的在前，含设备回执标记） |

⚠️ 这条控制通道**复用** `services.scenes.executor.execute_plan()`，
所以"高风险永不自动执行"这条红线在这里同样生效（不是靠提示词，是靠代码强制改写）。

已实现（设备管理）：
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/device-presets` | 可添加的预设设备清单（用户只能从这里选） |
| POST | `/api/devices` | 按预设新增一台设备（`high_risk` 由类型推导，不接受外部指定） |
| POST | `/api/devices/{device_id}/enabled` | 启用 / **停用（软删）** |

⚠️ `GET /api/device-presets` 刻意**不放在 `/api/devices/` 下面**：
`api/devices.py` 已注册 `GET /api/devices/{device_id}`，FastAPI 按声明顺序匹配，
`/devices/presets` 会被 `{device_id}` 先吃掉然后报 422。

已实现（M6 安防 / 照护 / 建议）：
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/security/alarms` | 告警列表（每条带判据、证据、建议处置） |
| POST | `/api/security/alarms/{id}/ack` | 人工确认（只记状态，不执行设备动作） |
| GET | `/api/care/observations` | 照护观察时间轴（非摄像头数据，结论为待确认线索） |
| GET | `/api/suggestions` | 主动建议（按打扰等级排序，含理由） |
| POST | `/api/suggestions/{id}/dismiss` | 忽略建议（12 小时内不再重复提） |
| POST | `/api/suggestions/{id}/accept` | 采纳建议（只记状态，不下发指令） |

⚠️ 这三组接口在返回前会**先跑一次幂等推导**（`sync_*`），所以"打开页面就有数据"，
不需要额外的后台任务。`?sync=false` 可以关掉（调试/压测用）。
数据来源必须说清楚：告警来自 `sensor_events` 与设备当前状态，
照护来自毫米波在场感知，建议来自告警/能耗/设备状态 —— **没有一处是编出来的**。

已实现（户型图自定义）：
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/floorplan` | 当前户型几何（含底图 URL 与"未画出的房间"） |
| PUT | `/api/floorplan` | 保存框选（房间码按 `rooms` 表校验） |
| POST | `/api/floorplan/image` | 上传底图（Pillow 真解码 + 归一化为 PNG） |
| GET | `/api/floorplan/image` | 取底图二进制 |
| POST | `/api/floorplan/reset` | 恢复默认（可只清框选、保留底图） |

⚠️ 几何存 `config/floorplan.json` 而不是数据库：`rooms` 表没有几何字段，
加字段要 DDL，而 `app_rw` 没有 DDL 权限（既定约束）。

后续里程碑（契约已在规划书 §10.3 冻结，此处保留占位说明）：
· `POST /api/imports/readings-csv`（首期受控 CSV 导入）
· `GET /api/audit-events`（管理端）
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    chat,
    confirmations,
    device_admin,
    device_commands,
    devices,
    energy,
    floorplan,
    health,
    homes,
    insights,
    knowledge,
    scenes,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(homes.router)
api_router.include_router(floorplan.router)
api_router.include_router(devices.router)
api_router.include_router(device_admin.presets_router)
api_router.include_router(device_admin.router)
api_router.include_router(device_admin.disabled_router)
api_router.include_router(device_commands.router)
api_router.include_router(energy.router)
api_router.include_router(knowledge.router)
api_router.include_router(scenes.router)
api_router.include_router(chat.router)
api_router.include_router(confirmations.router)
api_router.include_router(insights.security_router)
api_router.include_router(insights.care_router)
api_router.include_router(insights.suggestions_router)
