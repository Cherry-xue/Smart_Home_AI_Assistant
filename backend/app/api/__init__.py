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

后续里程碑（契约已在规划书 §10.3 冻结，此处保留占位说明）：
· `GET /api/security/alarms` · `GET /api/care/observations` · `GET /api/suggestions`（M6/M7）
· `GET /api/audit-events`（管理端）
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import chat, confirmations, devices, energy, health, homes, knowledge, scenes

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(homes.router)
api_router.include_router(devices.router)
api_router.include_router(energy.router)
api_router.include_router(knowledge.router)
api_router.include_router(scenes.router)
api_router.include_router(chat.router)
api_router.include_router(confirmations.router)
