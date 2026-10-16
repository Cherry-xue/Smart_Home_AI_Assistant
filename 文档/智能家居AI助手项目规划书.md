# 智能家居 AI 助手平台项目规划（多模态感知 + 多 Agent 协作 + 云边端协同）

> **依据**：《智能家居AI助手项目需求分析.md》（本项目需求基线）
> **形态**：前后端分离；后端 Python + FastAPI；前端 Vue 3 + Vite；边缘网关 Python（本机/树莓派）；Docker Compose 部署
> **首期边界**：MQTT 虚拟设备 + CSV/JSON 导入数据 + 只读态势感知 + 建议式控制；**不接管门锁、燃气阀等高风险设备的自动触发，不绕过高风险操作二次确认**
> **版本**：v1.0（规划稿） · **日期**：2026-10-13
> **定位说明**：本文是需求分析的落地侧延伸。需求分析回答"要做成什么样"，本文回答"怎么排布、怎么落地、先做哪一块、哪些坑要提前知道"。

## 1. 一句话定位

一个面向家庭的 **AI 管家中枢**：把灯光、空调、窗帘、门锁、摄像头、燃气/水浸/烟感、光伏储能等设备统一接入，用户用一句自然语言表达意图（"我要睡了""家里有点闷""这个月电费怎么这么高"），后端多个专业 Agent 分工协作——有的编排场景、有的守安防、有的算能耗、有的看护老人、有的当家务助理，输出**可执行的动作序列或待确认的建议**，所有高风险操作仍走二次确认与审计留痕。

```text
移动端 App / 中控屏 / Web 后台 (Vue 3)
        │ HTTP / SSE
        ▼
   FastAPI 接入层 ──鉴权 · 限流 · 设备范围过滤
        │
        ▼
   家庭协调 Agent（意图理解 · 任务路由 · 结果汇总）
        ├─ 场景编排与设备控制 Agent
        ├─ 安防与应急处置 Agent
        ├─ 能源优化 Agent
        ├─ 健康照护 Agent
        └─ 家务助理与主动服务 Agent
                │
   ┌────────────┼───────────────┬────────────────┐
   ▼            ▼               ▼                ▼
MySQL 8.0    Chroma          本地小模型        边缘网关（MQTT）
(设备/事件    (家庭记忆/      (Qwen 小参数量,   Home Assistant /
 /审计)       设备知识)      意图理解兜底)      Zigbee2MQTT / Matter
                                                    │
                                            MQTT 虚拟设备（首期）
```

## 1.1 与需求分析四层架构对齐

需求分析定义的是「感知与数据层 → 家庭认知引擎层 → 多智能体业务层 → 前端展示层」，本规划按前后端分离 + 边缘网关落地：

| 需求层 | 规划落地 | 首期重点 |
|---|---|---|
| 感知与数据层 | `gateway/`（MQTT 适配）+ 设备/遥测表 + 虚拟设备脚本 | 统一设备抽象（能力建模）、时间戳与质量标记、离线设备显式标注 |
| 家庭认知引擎层 | `app/agent/graph.py` + 记忆检索 + 本地小模型 | 意图理解、成员识别、任务拆解、云边端调度决策 |
| 多智能体业务层 | 1 个协调 Agent + 5 个专业 Agent | 白名单工具、结构化输出、高风险动作走确认节点 |
| 前端展示层 | Vue 3 多端界面 + 审核/审计 API | 场景卡片、安防时间轴、能耗看板、打扰分级开关、审计留痕 |

## 2. 为什么是"Agent + 长期记忆"而不是"规则引擎 + 语音助手"

智能家居要同时处理三类信息，性质完全不同：

| 信息类型 | 典型内容 | 适合的处理方式 | 只做一半会怎样 |
|---|---|---|---|
| 实时设备状态 | 灯/空调/门锁/传感器当前值、功率、环境数据 | 确定性查询与命令下发 | — |
| 家庭知识 | 成员偏好、禁忌、作息、设备能力、房屋布局 | 向量检索（RAG）+ 结构化档案 | — |
| **混合请求** | "我要睡了，但我明早六点要起" | 先查成员作息与禁忌 → 判断季节与室温 → 编排多步动作 → 检查安防 → 输出并确认 | 只会规则引擎：无法理解"有点闷"；只会大模型：会瞎编设备不存在的功能 |

**真正有价值的是第三类混合请求**，这也是普通智能音箱最不擅长的地方。所以本项目用 Agent 做规划与工具编排，用长期记忆做个性化，**具体数值与设备能力校验仍由确定性代码完成，不让模型心算或臆造设备功能**。

### 三类典型问题（就是首期验收用例）

| 难度 | 用户问法 | 期望走的路径 |
|---|---|---|
| 简单 | "客厅现在温度多少？" | 只查设备遥测，返回数值、单位、更新时间与是否在线 |
| 中等 | "把家里调成看电影的样子" | 检索"观影模式"定义 → 映射设备能力清单 → 冲突检测（剔除"主灯全亮"）→ 输出动作序列 |
| 中等 | "这个月电费为什么高了？" | 查分时用能曲线 → 对比上月与去年同期 → 定位高耗设备 → 给出可执行建议（带计算口径） |
| **困难** | "我妈今天好像一直没动，帮我看看" | 查活动传感器时序 → 判断异常时长 → 检索家庭应急预案 → 生成带假设的处置建议 → 不自动拨打，转人工确认 |
| 风险边界 | "把门锁打开""把燃气阀关掉" | 拒绝自动执行，只生成待确认建议；必须走二次确认与授权流程 |

以上回答只用于家庭辅助决策，不承诺在缺少真实房屋拓扑、设备能力档案或历史数据时给出准确判断。

## 3. 场景设定与首期范围

### 首期要做

- 建立经用户确认的家庭设备清单与能力档案（设备类型、可控参数、取值范围、是否高风险）；
- 用 MQTT 虚拟设备 + 导入数据完成全屋态势展示（设备状态、环境、能耗、告警）；
- 支持自然语言创建/修改场景，自动做设备能力映射与冲突检测；
- 支持家庭成员档案与偏好记忆（谁在说话、按谁的习惯来）；
- 提供 5 个专业 Agent 协作下的问答、编排、诊断、能耗分析与建议生成；
- 能耗分析给出**可追溯到输入曲线与计算口径**的建议，不做无依据估算；
- 高风险操作（门锁、燃气、安防布防撤防）**只输出待确认建议**，走二次确认与审计；
- 打扰分级可配置（紧急/重要/普通/静默），支持免打扰时段与一键关闭主动建议；
- 保存用户指令、Agent 决策依据、设备动作、人工确认与审计记录。

### 首期明确不做

- 不接管门锁、燃气阀、电气总闸等高风险设备的**自动触发**（只能建议 + 人工确认）；
- 不替代既有安防系统、消防联动、电梯/水电应急等有资质要求的专业系统；
- 不在未完成设备能力摸底与房屋拓扑确认前承诺精确的能耗预测或舒适度优化；
- 不假设未确认的设备型号、房间布局、传感器位置或成员作息；
- 不把模型推测写成"已发生设备故障""已确认老人跌倒"等事实性结论。

### 第二阶段候选能力

- 接入真实 Matter / Zigbee2MQTT 设备，完成真机联调与延迟实测；
- 用真实历史数据校准能耗基线、异常阈值与舒适度模型；
- 评估与社区物业、紧急联系人、家政服务的接口；
- 多智能体冲突协商（省电 vs 舒适、个性 vs 多用户）的完整策略引擎；
- 情感陪伴、无障碍深度适配——**如涉及，需单独立项评估伦理与依赖风险**。

## 4. 需求分析到系统模块映射

| 需求能力 | 首期系统模块 | 主要数据/证据 | 输出 |
|---|---|---|---|
| 全屋态势监控 | 设备抽象层 + 态势看板 | 设备状态、环境数据、在线/离线、质量标记 | 房间视图、指标、更新时间 |
| 场景编排 | 场景编排与设备控制 Agent | 场景定义、设备能力档案、成员偏好 | 动作序列、冲突检测结果 |
| 安防应急 | 安防与应急处置 Agent | 传感器事件、门锁状态、摄像头（可选） | 分级告警、处置时间轴、二次确认项 |
| 能耗优化 | 能源优化 Agent + 用能统计服务 | 分时电价、功率曲线、光伏/储能、历史账单 | 账单对比、高耗定位、省费建议 |
| 健康照护 | 健康照护 Agent | 活动传感器、用药打卡、门磁节律、水电使用 | 异常预警、处置建议、家属通知草稿 |
| 家务助理与主动服务 | 家务助理 Agent + 打扰分级服务 | 库存识别、日历、待办、用户画像 | 建议卡片（含推荐原因与打扰等级） |
| 个性化记忆 | 记忆检索工具 + 成员档案 | 偏好、禁忌、作息、历史反馈 | 个性化结果与"为什么这样推荐"依据 |
| 权限与审计 | 鉴权、角色、设备范围过滤 | 角色、家庭范围、操作日志 | 最小权限访问与完整审计 |
| 多用户冲突处理 | 成员识别 + 优先级规则 | 说话人、角色优先级、场景规则 | 协商结果与冲突说明 |
| 移动端交互 | Vue + SSE 对话界面 | 会话、指令、执行结果 | 流式回答、动作确认 |

### 4.1 六个 Agent 职责（与需求对齐）

各 Agent 仅使用授权设备与白名单工具，输出结构化结论；协调 Agent 负责路由与汇总。

| Agent | 职责要点 | 首期约束 |
|---|---|---|
| 0. 家庭协调与任务路由 | 识别意图、识别发言人、判断任务类型与涉及房间/成员；汇总结果并送入确认流程 | 不直接下发高风险设备指令；信息不足时补问或转人工 |
| 1. 场景编排与设备控制 | 自然语言→场景定义→设备能力映射→冲突检测→动作序列；执行失败重试与降级 | 设备能力未确认处必须标注限制；不臆造设备功能 |
| 2. 安防与应急处置 | 入侵、燃气、水浸、烟感、门窗未关的分级告警与联动处置时间轴 | 高风险联动只生成待确认项，不自动执行；不得仅凭单一传感器认定事故 |
| 3. 能源优化 | 分时电价 + 光伏 + 储能 + 充电需求的用能调度建议与账单分析 | 数据不足时不得给出确定性省费金额 |
| 4. 健康照护 | 老人活动异常、跌倒线索、用药提醒、儿童作息；优先非摄像头手段 | 涉及健康结论必须标注为"待确认线索"，不得断言诊断 |
| 5. 家务助理与主动服务 | 库存/耗材、补货建议、日程协同、提醒；按打扰分级推送 | 主动建议需可解释、可关闭；静默时段不得打扰 |

### 4.2 核心业务闭环（需求流程落地）

1. 数据从 MQTT 虚拟设备、导入文件或真实网关接入（首期 mock/CSV）；
2. 按已确认的设备档案与房间拓扑展示全屋态势，校验时间、单位、在线状态与数据质量；
3. 用户以语音/文字/手势发起请求，或由传感器事件自动触发；
4. 家庭协调 Agent 判定意图、识别发言成员、拆解任务；
5. 按需调用编排/安防/能耗/照护/助理 Agent，检索家庭记忆与设备知识；
6. 输出结论：动作序列或建议、决策依据、风险、不确定性与记忆引用来源；
7. 对门锁、燃气、布防撤防等**仅生成待确认建议**，不自动执行；
8. 人工确认/驳回后记录确认人、时间、依据与执行结果；
9. 更新设备状态与家庭记忆；未闭环问题进入告警、待办或次日提醒。

### 4.3 项目创新点（规划侧回应）

1. **云边端协同的认知架构**：本地实时处理保障隐私与断网可用，云端大模型负责复杂推理；
2. **长期记忆驱动的个性化**：偏好、禁忌、作息沉淀为可检索记忆，且每条建议可回溯到记忆来源；
3. **多 Agent 专业协作**：编排、安防、能耗、照护、助理职责解耦，可独立演进、可团队分工开发；
4. **冲突目标协商**：省电 vs 舒适、个性 vs 多成员偏好，通过角色优先级 + 场景规则 + 协商裁决；
5. **主动服务但可控可解释**：打扰分级、免打扰时段、一键关闭，解决"越智能越烦人"；
6. **高风险操作双保险**：二次确认 + 白名单 + 审计留痕，模型不直接触发门锁燃气。

## 5. 技术栈与环境

首期以**易部署、可离线、可替换**为原则，不把系统绑定到单一云厂商或单一设备生态。

### 后端 Python（容器 backend）

```text
Python 3.11
FastAPI + Uvicorn                  # REST API、健康检查、SSE 流式回答
Pydantic 2                         # 请求/响应模型与设备档案校验
SQLAlchemy 2 async + asyncmy       # 异步 MySQL 8.0 访问
Alembic                            # 数据库迁移
paho-mqtt                          # MQTT 客户端（订阅设备状态、下发指令）
LangGraph / LangChain              # Agent 工作流与工具调用
MySQL 8.0                          # 设备、事件、记忆、审计
Chroma                             # 家庭记忆与设备知识向量库（与业务库分离）
APScheduler（可选）                # 定时任务：电价时段、巡检、日报
pypdf / python-docx                # 设备说明书、保修单解析
```

> 模型客户端、Embedding 服务通过环境变量配置。**本地优先**：意图理解用本地小模型（Qwen 小参数量或 Ollama 承载），复杂推理或无本地算力时回退云端 API；模型绝不能访问设备控制接口。

### 前端 Node（容器 frontend）

```text
Node 20
Vue 3 + Vite
TypeScript                         # 可选，建议用于接口类型约束
Pinia                              # 家庭会话与界面状态
Element Plus                       # 卡片、时间轴、抽屉、消息提示
ECharts                            # 能耗曲线、分时电价、房间态势图
fetch + ReadableStream             # SSE 流式回答与中止
```

### 边缘网关 / 设备层

```text
Home Assistant（开发可用 Docker 版）  # 设备统一接入与抽象
Mosquitto                            # MQTT Broker
Zigbee2MQTT / Matter（第二阶段）     # 真实设备协议
树莓派 5（可选）                      # 边缘常驻节点的最低成本承载
```

### 数据库、容器与部署

```text
MySQL 8.0（设备/事件/记忆/审计）+ Chroma（向量）
Docker + Docker Compose v2
Nginx（前端静态资源与 /api 反向代理）
```

首期可在开发机用 Compose 一键起五个服务（含 MQTT 虚拟设备）；真实家庭部署时的网络分区、账号认证、日志留存与隐私合规以当地法规与家庭约定为准。

## 6. 分层结构与容器拓扑

| 层 | 职责 | 首期实现 |
|---|---|---|
| 展示层 | 全屋态势、房间卡片、对话窗口、能耗看板、安防时间轴 | Vue 3 + ECharts |
| 接入层 | 鉴权、REST、SSE、设备范围过滤、限流 | FastAPI |
| Agent 编排层 | 意图识别、任务路由、工具调用、确认暂停节点 | LangGraph 状态图 |
| 工具层 | 设备查询（只读）、场景下发（分级）、记忆检索、能耗计算、报告生成 | 明确白名单工具 |
| 数据保护层 | 权限过滤、高风险动作拦截、二次确认、审计 | 后端强制校验 |
| 数据层 | MySQL 8.0、Chroma 数据卷、导入文件 | Docker 卷 |
| 设备适配层 | MQTT 虚拟设备；后续 Home Assistant / Matter | 接口隔离，默认只开虚拟设备 |

```text
Browser / App
  │ HTTPS / SSE
  ▼
Nginx ── /api ──> FastAPI
                    ├── LangGraph Agent ──> read_device_state / list_capability
                    │                    ├─> search_memory / search_device_docs
                    │                    ├─> calc_energy_cost / draft_report
                    │                    └─> propose_action（高风险仅生成待确认项）
                    ├── MySQL 8.0
                    ├── Chroma（家庭记忆 / 设备知识）
                    └── MQTT（Mosquitto）──> 虚拟设备 / Home Assistant

禁止存在：Agent ──> 门锁/燃气 自动执行接口（只能 propose_action → 人工确认）
```

## 7. 数据库设计

> **选型说明**：业务数据（设备档案、事件、审计）统一用 **MySQL 8.0**（`utf8mb4`），理由与示例一致——**字段 COMMENT 会被 LangChain 的 `get_table_info()` 拼进提示词**，是提升 NL2SQL / 设备查询准确率性价比最高的一招；同时 MySQL 的 `GRANT SELECT` 能把护栏从应用层下沉到数据库层。向量检索用独立 **Chroma**（开发/小规模）。

首期重点是保存**可解释、可追溯**的数据关系，而非功能堆砌。下面是逻辑表建议。

### 7.1 基础对象（家庭与设备）

| 表 | 关键字段 | 用途 |
|---|---|---|
| homes | id, name, timezone, address_hash, created_at | 家庭主体（地址脱敏存储） |
| rooms | id, home_id, name, floor, sort_no | 房间/区域（客厅、主卧、厨房…） |
| members | id, home_id, nickname, role_code, birth_year, enabled | 家庭成员（户主/老人/儿童/访客） |
| devices | id, home_id, room_id, name, device_type, protocol, vendor, high_risk, enabled | 设备实例（含是否高风险标记） |
| device_capabilities | device_id, capability_code, param, unit, value_min, value_max, writable | **设备能力档案**——防止模型臆造不存在的能力 |
| member_preferences | member_id, capability_code, value, weight, source, updated_at | 成员偏好（温度、亮度、作息） |
| user_roles | user_id, member_id, role_code, room_scope, can_confirm_high_risk | 角色与数据/设备范围 |

### 7.2 运行态与事件

| 表 | 关键字段 | 用途 |
|---|---|---|
| device_readings | device_id, observed_at, value, unit, quality, is_stale, source | 遥测值、质量标记、来源；**过期/缺失不得伪装实时值** |
| device_commands | id, device_id, command, params_json, issued_by, risk_level, status, confirmed_by, confirmed_at | 指令记录；高风险指令 status 必须经过 confirmed 才可执行 |
| scenes | id, home_id, name, trigger_type, definition_json, created_by, enabled | 场景定义（睡眠/观影/起床/离家） |
| scene_runs | id, scene_id, started_at, finished_at, steps_json, failed_steps, fallback_used | 场景执行记录（含降级情况） |
| sensor_events | id, device_id, event_code, level, started_at, ended_at, payload | 传感器事件（入侵、水浸、烟感…） |
| alarms | id, room_id, alarm_code, level, status, acknowledged_by, acknowledged_at | 告警及确认状态 |
| energy_readings | device_id, observed_at, power_w, energy_kwh, tariff_period | 用能明细（支持分时电价） |
| care_observations | member_id, observed_at, activity_type, duration_min, source_device | 照护观察（活动节律、用药打卡） |

### 7.3 知识、记忆与审计

| 表 | 关键字段 | 用途 |
|---|---|---|
| knowledge_documents | id, title, doc_type, version, valid_from, valid_to, status, approved_by | 设备说明书、保修单、安防预案元数据；status 含草稿/现行/废止 |
| knowledge_chunks | id, document_id, page_or_section, content, embedding, room_scope | 检索片段与原文定位 |
| memories | id, home_id, member_id, memory_type, content, embedding, source, confidence, decay_at | **长期记忆**（偏好/禁忌/事件），含来源与置信度，支持遗忘策略 |
| suggestions | id, home_id, member_id, content, reason_json, disturbance_level, status, dismissed_at | 主动建议（含推荐原因与打扰等级） |
| disturbance_rules | id, home_id, member_id, level, quiet_start, quiet_end, enabled | 免打扰配置 |
| conversations | id, user_id, created_at, title | 用户会话 |
| agent_runs | id, conversation_id, intent, tools_used, status, duration_ms | Agent 执行记录与工具调用摘要 |
| audit_events | id, user_id, action, object_type, object_id, created_at | 查看、导出、确认与配置变更日志 |

高频遥测数据的保留周期与降采样策略应在数据摸底后确定；**不得凭空设置生产告警阈值**。

## 8. 目录结构

```text
smart-home-agent/
├── README.md                      # 一键起服说明
├── Makefile                       # make up / make down / make logs / make sim
├── docker-compose.yml             # 六个服务：frontend / backend / mysql / chroma / mqtt / simulator
├── .env                           # 唯一环境变量来源，不进 git
├── .env.example
├── .gitignore
│
├── mysql/
│   └── init/
│       ├── 01_schema.sql          # 建库 + 数据表
│       ├── 02_readonly_user.sql   # 只读账号（护栏第 1 层）
│       └── 03_seed.sql            # 房屋/设备/成员模拟数据（两室一厅 + 30 台虚拟设备）
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py                # FastAPI 入口 + CORS + 生命周期
│   │   ├── api/
│   │   │   ├── chat.py            # POST /api/chat、/api/chat/stream(SSE)
│   │   │   ├── devices.py         # 设备状态、能力、房间视图
│   │   │   ├── scenes.py          # 场景 CRUD 与执行
│   │   │   ├── energy.py          # 能耗与账单分析
│   │   │   ├── security.py        # 告警与处置时间轴
│   │   │   └── health.py          # GET /api/health
│   │   ├── core/
│   │   │   ├── config.py          # pydantic-settings 读环境变量
│   │   │   ├── auth.py            # 鉴权与角色范围
│   │   │   ├── limits.py          # 并发闸门与限流
│   │   │   └── callbacks.py       # BaseCallbackHandler：token / 耗时
│   │   ├── models/
│   │   │   └── llm.py             # get_llm() / get_embeddings()（本地优先 + 云端回退）
│   │   ├── db/
│   │   │   └── connection.py      # engine + SQLDatabase + 四层护栏
│   │   ├── rag/
│   │   │   ├── ingest.py          # 说明书/预案 → 切分 → 向量化 → Chroma
│   │   │   └── retriever.py       # MMR + 阈值 + 元数据过滤（按房间/成员）
│   │   ├── tools/
│   │   │   ├── device_tools.py    # @tool read_device_state / list_capability
│   │   │   ├── memory_tools.py    # @tool search_memory / save_memory
│   │   │   ├── energy_tools.py    # @tool calc_energy_cost
│   │   │   └── action_tools.py    # @tool propose_action（含高风险拦截）
│   │   ├── agent/
│   │   │   ├── graph.py           # 协调 Agent + 5 个专业 Agent 组装
│   │   │   ├── middleware.py      # 限流 + 确认暂停 + 日志
│   │   │   └── prompts.py         # system prompt + 设备能力约束 + 安全边界
│   │   └── schemas/                # Pydantic 请求/响应模型
│   └── data/
│       └── raw/                    # 设备说明书、安防预案（开发样例）
│
├── frontend/
│   ├── Dockerfile                  # 两阶段：node 构建 → nginx 运行
│   ├── nginx.conf                  # 反代 /api → backend:8000
│   ├── vite.config.js
│   └── src/
│       ├── App.vue
│       ├── api/                    # chat.js / devices.js / energy.js
│       ├── views/                  # Home, Devices, Energy, Security, Care, Reports
│       └── components/
│           ├── RoomCard.vue        # 房间态势卡片
│           ├── ChatWindow.vue      # 对话窗口（含风险提示）
│           ├── SceneBuilder.vue    # 场景编排（自然语言 → 动作序列）
│           ├── EnergyChart.vue     # 分时电价 + 用能曲线
│           └── ConfirmDialog.vue   # 高风险动作二次确认
│
├── gateway/
│   ├── simulator/                  # MQTT 虚拟设备（30 台）
│   │   ├── emit_readings.py        # 周期性上报温湿度/功率/状态
│   │   └── device_profiles.yaml    # 虚拟设备能力档案
│   └── adapters/                   # 后续 Home Assistant / Matter 适配
│
├── evals/
│   └── cases.yaml                  # 20+ 验收用例
├── compose.yaml
└── docs/                           # 数据字典、接口与部署说明
```

现场真实家庭数据（成员作息、门锁日志）不应放进 Git 仓库；通过受控数据卷接入。

## 9. 八个关键设计决策

1. **先做"建议式"系统**：首期只读写虚拟设备，门锁/燃气类动作只生成待确认项，不自动执行；
2. **设备能力档案先行**：模型必须依据 `device_capabilities` 判断"能不能做、取值多少"，杜绝臆造出设备没有的功能；
3. **数值计算交给确定性代码**：能耗汇总、费用、变化率、异常时长由后端算，不让大模型心算；
4. **记忆必须有来源与置信度**：每条记忆带 source / confidence / decay_at，可被用户查看、修正、删除；
5. **Agent 用白名单工具**：每个工具只开放完成任务所需的最小读写范围，高风险工具单独隔离，调用写审计；
6. **建议和事实分开显示**：界面分开展示设备实测值、规则计算结果、模型推测、人工确认与最终处置；
7. **先用虚拟设备验证流程**：没完成真机与网络部署前，不假设 Matter/Zigbee 可用，也不采集真实隐私数据；
8. **不在首期堆复杂组件**：情感陪伴、自动补货支付、复杂预测模型作为后续评估项，不影响 MVP 闭环。

## 10. 前端设计（Vue 3 + Vite）

### 10.1 页面清单

- **家庭总览**：房间卡片墙，每间房显示关键指标、设备在线数、告警摘要与数据更新时间；
- **设备与房间**：按房间筛选设备，点击查看能力档案、当前值、历史曲线与是否高风险；
- **对话助手**：自然语言提问、流式回答、检索过程、记忆引用侧栏；高风险请求显示安全提示与确认入口；
- **场景编排**：左侧自然语言输入，右侧生成动作序列与冲突检测结果；支持存为场景、手动微调；
- **能耗看板**：分时电价曲线 + 24 小时用能曲线 + 账单对比 + 建议卡片（点击展开"为什么这么建议"）；
- **安防应急**：平面示意图 + 传感器节点 + 告警时间轴 + 处置记录；
- **健康照护**：活动节律时间轴、异常事件卡、用药打卡记录（**默认使用非摄像头数据源**）；
- **建议中心**：主动建议信息流，每条标出推荐原因与打扰等级；顶部免打扰开关与分级设置；
- **知识库（管理端）**：设备说明书、保修单、安防预案的导入、版本与检索；
- **用户与权限（管理端）**：成员角色、房间范围、高风险确认权限、审计日志查询。

### 10.2 回答呈现规范

回答内容固定分区，避免"模型说什么就是什么"：

1. **观测事实**：设备值、时间、单位、在线状态与数据质量；
2. **分析结果**：计算口径与对比对象（如"对比上周同期"）；
3. **记忆依据**：引用了哪条偏好/禁忌，来自哪次交互或哪次设置；
4. **可能原因/建议**：明确标注为辅助判断；
5. **待确认项**：缺少的数据、未确认的设备能力或需要人工核实的现象。

对门锁、燃气、布防撤防等请求，前端显示安全提示与人工确认流程，**不出现"已执行"字样**。

### 10.3 接口契约（前后端边界）

```text
GET  /api/health
GET  /api/homes/{home_id}/rooms
GET  /api/devices?room_id=&type=
GET  /api/devices/{device_id}/readings?from=&to=
GET  /api/devices/{device_id}/capabilities
POST /api/scenes/preview                  # 自然语言 → 动作序列 + 冲突检测（不执行）
POST /api/scenes                          # 保存场景
POST /api/scenes/{scene_id}/run           # 执行（高风险步骤返回待确认）
POST /api/chat/stream                     # SSE；输入问题与会话 id
POST /api/confirmations                   # 高风险动作二次确认记录
GET  /api/energy/summary?from=&to=        # 账单、分时用能、高耗设备
GET  /api/security/alarms?status=&room_id=
GET  /api/care/observations?member_id=&from=&to=
GET  /api/suggestions?level=              # 主动建议（按打扰等级过滤）
POST /api/imports/readings-csv            # 首期受控 CSV 导入
GET  /api/audit-events                    # 审计查询（需管理员权限）
```

所有接口使用统一时间格式与单位；API 返回 `observed_at`、`source`、`quality`、`is_stale` 等元数据。确认接口只记录意见与状态，**不直接触发设备**。

## 11. Docker 部署

### 11.1 开发环境 Compose 服务

```yaml
services:
  mysql:
    image: mysql:8.0
    command: >
      --character-set-server=utf8mb4
      --collation-server=utf8mb4_general_ci
      --default-authentication-plugin=mysql_native_password
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
      MYSQL_DATABASE: smart_home
    volumes:
      - mysql_data:/var/lib/mysql
      - ./mysql/init:/docker-entrypoint-initdb.d:ro   # 只在首次初始化时执行
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1", "-uroot", "-p${MYSQL_ROOT_PASSWORD}"]
      interval: 5s
      timeout: 3s
      retries: 20
    restart: unless-stopped

  mqtt:
    image: eclipse-mosquitto:2
    ports:
      - "1883:1883"
    volumes:
      - ./gateway/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
    restart: unless-stopped

  backend:
    build: { context: ./backend, dockerfile: Dockerfile }
    environment:
      MYSQL_URI: mysql+asyncmy://agent_ro:${AGENT_RO_PASSWORD}@mysql:3306/smart_home?charset=utf8mb4
      MQTT_HOST: mqtt
      LLM_BASE_URL: ${LLM_BASE_URL}
      LLM_API_KEY: ${LLM_API_KEY}
      LLM_MODEL: ${LLM_MODEL}
      DATA_MODE: mock                 # mock / csv / real_gateway
      HIGH_RISK_AUTO_EXEC: "false"    # 高风险自动执行：默认关闭
    ports:
      - "8000:8000"
    volumes:
      - ./chroma_data:/app/chroma_data
      - ./backend/data:/app/data
    depends_on:
      mysql: { condition: service_healthy }
    restart: unless-stopped

  simulator:
    build: { context: ./gateway/simulator, dockerfile: Dockerfile }
    environment:
      MQTT_HOST: mqtt
      HOME_ID: "1"
    depends_on:
      - mqtt
    restart: unless-stopped

  frontend:
    build: { context: ./frontend, dockerfile: Dockerfile }
    ports:
      - "80:80"
    depends_on:
      - backend
    restart: unless-stopped

volumes:
  mysql_data:
```

### 11.2 配置项示例

```text
APP_ENV=development
DATABASE_URL=mysql+asyncmy://agent_ro:pass@mysql:3306/smart_home?charset=utf8mb4
MQTT_HOST=mqtt
MQTT_PORT=1883
LLM_BASE_URL=...          # 本地模型（Ollama/vLLM）或云端兼容接口
LLM_API_KEY=...
LLM_MODEL=...
EMBEDDING_MODEL=...
DATA_MODE=mock            # mock / csv / real_gateway
HIGH_RISK_AUTO_EXEC=false # 门锁/燃气自动执行，默认关闭，需人工确认
QUIET_HOURS_DEFAULT=22:00-07:00
```

密钥通过部署环境注入，不写入仓库。`HIGH_RISK_AUTO_EXEC` 默认关闭；任何真实设备接入必须经过安全评估并单独配置白名单设备与房间范围。

### 11.3 上线检查清单

- 生产密钥与 `.env.example` 分离，数据库账号权限最小化（只读账号 `GRANT SELECT`）；
- 高风险设备（门锁、燃气）自动执行通道**默认关闭且不可被 prompt 绕过**；
- 启动日志、权限审计与备份策略已验证；
- 设备档案、房间拓扑、单位与数据质量规则经用户确认；
- 虚拟设备与真实设备在界面中明确区分；
- 模型服务不可用时，设备控制与安防告警仍可独立工作，服务降级；
- 废止/待审的设备说明书与预案不能进入正式检索集合；
- 摄像头默认关闭或仅本地处理，隐私相关说明对家庭成员可见。

## 12. 异步与并发设计

> 这一章回答两件事：**哪些环节可以异步**、**哪些异步了反而更糟**。

### 12.1 先把两件事分开：延迟 vs 吞吐

| 目标 | 含义 | 靠什么解决 |
|---|---|---|
| **降低单请求延迟** | 一个人说一句，多久出第一个字 / 执行完动作 | 缩短单次损耗、并行化可并行步骤、流式输出 |
| **提高并发吞吐** | 全家 5~8 人同时用，系统还扛不扛得住 | 异步 + 连接池 + 限流背压 |

**异步主要解决第二个问题。** 对于单条复杂指令（"我要睡了"），总延迟 ≈ 各轮 LLM 往返之和，异步压不掉这个串行依赖——第 2 步必须知道第 1 步结果。

### 12.2 时间到底花在哪了

一条典型复杂指令（"我要睡了，但明早六点要起"）耗时剖面，按本地小模型 + 云端回退估：

| 环节 | 次数 | 单次耗时 | 小计 | 性质 |
|---|---|---|---|---|
| 语音 ASR（若走语音） | 1 | ~600 ms | 600 ms | 本地/网络 |
| 成员识别 + 意图理解 | 1 | ~800 ms | 800 ms | 本地小模型 |
| 记忆检索（偏好/禁忌） | 1 | ~300 ms | 300 ms | 向量检索 |
| 设备状态查询 | 1 | ~50 ms | 50 ms | MQTT / MySQL I/O |
| 动作序列生成 | 1 | ~1.5 s | 1.5 s | LLM 往返 |
| 冲突检测与执行 | 1 | ~400 ms | 400 ms | 确定性代码 + MQTT |
| **合计** | | | **≈ 3.7 s** | 网络与推理占绝大多数 |

**结论**：时间几乎全花在等网络/推理，CPU 近乎空转——这正是 asyncio 的主场。

### 12.3 各环节能不能异步：总表

| 环节 | 现状 | 改成 | 收益 | 难度 |
|---|---|---|---|---|
| **FastAPI handler** | def（跑线程池） | async def | **高**，且是前提 | 低 |
| **SSE 流式** | 同步生成器占线程 | async def + astream | **极高** | 低 |
| **LLM 调用** | invoke | ainvoke / astream | **高**，原生 async | 低 |
| **Agent 编排** | agent.invoke | agent.ainvoke | 释放事件循环 | 低 |
| **多设备并发查询** | 逐个串行 | asyncio.gather 并发 | **中高** | 低 |
| **MySQL 执行** | pymysql（阻塞） | asyncmy 异步池 | 中 | 中 |
| **MQTT 批量下发** | 逐条 publish | 批量/并发 publish | 中 | 低 |
| **Chroma 检索** | 同步 client | AsyncClient / to_thread | 中 | 中 |
| **Embedding** | 同步 SDK | to_thread 包一层 | 中 | 低 |
| **设备说明书解析** | 同步 CPU | **不要 async**，线程池或离线跑 | — | — |
| **向量库灌库（离线）** | 逐条 embedding | **批量 embedding** | **极高** | 低 |

### 12.4 最关键的收益：流式不再占住线程

**这是最容易踩、代价也最大的一处。** 顺着直觉写出来的第一版基本都长这样：

```python
# ❌ 同步生成器：一个用户占住一个线程 3~4 秒
@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    def gen():
        for chunk in agent.stream(...):
            yield f"data: {json.dumps({'delta': chunk})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
```

家庭场景并发不高（5~8 人），但**一旦开了定时巡检、能耗日报、周末离家自动布防这些后台任务，再加上中控屏长连接**，同步写法就会开始排队。正确做法是 `async def` + `astream`，见 13.5。

### 12.5 一条铁律：async 里绝对不能有阻塞调用

`paho-mqtt` 的 `publish`、`pymysql` 的 `execute`、`time.sleep()` 都是阻塞的。在 `async def` 里直接调用会卡住整个事件循环：
- MySQL → 用 `asyncmy`
- MQTT → 用 `aiomqtt`，或把 publish 包进 `asyncio.to_thread`
- CPU 类的解析 → `run_in_executor`

### 12.6 并发不是无限的：必须加背压

```python
# backend/app/core/limits.py
import asyncio

# 同时跑多少个 Agent（按家庭规模，先保守设 4，压测后再加）
AGENT_SEMAPHORE = asyncio.Semaphore(4)

# 单次会话最大工具调用轮数（防死循环烧钱）
MAX_AGENT_STEPS = 8
```

同时给时序查询限定最大时间窗口与返回行数，防止"查这个月所有设备每秒数据"拖垮数据库。

### 12.7 取消传播：用户点"停止"要真的停

中控屏上取消对话、或语音被新指令打断时，要取消下游模型请求与工具任务，并**确保已下发的设备指令不被半途中断**（要么不下发，要么下发后记录结果）——这是家庭场景特有的约束，跟纯问答系统不一样。

## 13. 关键代码骨架

以下都是可直接落地的骨架。

### 13.1 模型单例 backend/app/models/llm.py

```python
import os
from langchain_openai import ChatOpenAI
from langchain_community.embeddings.dashscope import DashScopeEmbeddings
from dotenv import load_dotenv

load_dotenv()


def get_llm(model: str = None, temperature: float = 0.0) -> ChatOpenAI:
    """本地优先：本地模型接不上时自动回退云端，靠 base_url 区分。"""
    return ChatOpenAI(
        api_key=os.getenv("LLM_API_KEY"),
        base_url=os.getenv("LLM_BASE_URL"),
        model=model or os.getenv("LLM_MODEL", "qwen2.5-7b-instruct"),
        temperature=temperature,
    )


def get_embeddings() -> DashScopeEmbeddings:
    return DashScopeEmbeddings(
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
        dashscope_api_key=os.getenv("LLM_API_KEY"),
    )
```

`temperature=0` 很关键 —— 处理设备控制要的是确定性，不是创意。

### 13.2 建记忆向量库 backend/app/rag/ingest.py

```python
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from app.models.llm import get_embeddings
from app.core.config import settings

loader = PyPDFLoader(settings.device_doc_path)      # 设备说明书/安防预案
docs = loader.load()

splitter = RecursiveCharacterTextSplitter(
    chunk_size=800, chunk_overlap=100, add_start_index=True,
)
chunks = splitter.split_documents(docs)

# 打元数据：后面按房间/成员过滤全靠它
for d in chunks:
    d.metadata.setdefault("doc_type", "device_manual")
    d.metadata.setdefault("room_scope", "all")

Chroma.from_documents(
    chunks, get_embeddings(), persist_directory=settings.chroma_dir
)
print("入库完成，chunk 数:", len(chunks))
```

> 容器里这么跑：`docker compose exec backend python -m app.rag.ingest`。`chroma_data/` 挂了数据卷，**容器重建不用重跑**（跑一次要花 Embedding 的钱）。批量 embedding 见 12.3 最后一行。

### 13.3 设备状态工具 backend/app/tools/device_tools.py

```python
from langchain_core.tools import tool
from app.db.connection import read_devices, read_capabilities


@tool
async def read_device_state(room_name: str, device_type: str = "") -> str:
    """查询指定房间的设备当前状态（温度、亮度、开关、功率等）。
    当用户询问"现在怎么样""是否开着""多少度"这类现状问题时使用。
    返回设备名、当前值、单位、更新时间与在线状态。"""
    rows = await read_devices(room_name, device_type)
    if not rows:
        return f"未找到 {room_name} 的{'该类' if device_type else ''}设备。请勿编造设备。"
    out = []
    for r in rows:
        stale = "（数据已过期，不可作为实时值）" if r["is_stale"] else ""
        out.append(f"{r['name']}：{r['value']}{r['unit']}，更新于 {r['observed_at']}{stale}")
    return "\n".join(out)


@tool
async def list_capability(room_name: str, device_type: str) -> str:
    """查询某类设备**支持哪些能力以及取值范围**。在生成控制指令前必须调用，
    用来确认设备到底能不能做这件事、参数允许到多少。不要在未调用本工具时
    假设设备具备某个功能。"""
    caps = await read_capabilities(room_name, device_type)
    if not caps:
        return "该设备无已登记的能力档案，不得对其下发控制指令。"
    return "\n".join(f"{c['capability_code']}({c['unit']}): {c['value_min']}~{c['value_max']}" for c in caps)
```

> **docstring 是这个工具的说明书**。Agent 靠读它决定要不要调用、什么时候调用——写得含糊，Agent 就会跳过它去臆造设备功能，这是智能家居最危险的失败模式。

### 13.4 高风险动作工具 backend/app/tools/action_tools.py

```python
from langchain_core.tools import tool
from app.db.connection import create_command


HIGH_RISK_TYPES = {"door_lock", "gas_valve", "main_breaker", "security_arm"}


@tool
async def propose_action(device_id: int, command: str, params: dict) -> str:
    """向设备提出一个动作。**本工具不会真的执行高风险操作**：
    门锁、燃气阀、电气总闸、布防撤防一律只生成"待确认建议"，
    由授权家庭成员在前端二次确认后才会生效。低风险动作（灯光、窗帘、空调）
    可直接提交执行。返回该动作的当前状态。"""
    rec = await create_command(device_id, command, params)
    if rec["risk_level"] == "high":
        return (
            f"已生成待确认建议（#{rec['id']}）：{rec['device_name']} {command}。"
            "高风险操作需由授权成员二次确认后执行，请勿向用户声称已执行。"
        )
    return f"已下发（#{rec['id']}）：{rec['device_name']} {command}，状态 {rec['status']}。"
```

工具层之上还要有一道后端强校验（见 8.1 的四层护栏思路）：即使模型绕过工具描述直接构造高风险指令，`create_command` 也会把 `risk_level=high` 的记录强制置为 `pending_confirm`。

### 13.5 Agent 组装与流式调用 backend/app/agent/graph.py

```python
from langgraph.prebuilt import create_react_agent
from app.models.llm import get_llm
from app.tools.device_tools import read_device_state, list_capability
from app.tools.memory_tools import search_memory, save_memory
from app.tools.energy_tools import calc_energy_cost
from app.tools.action_tools import propose_action
from app.agent.prompts import SYSTEM_PROMPT


# 五个工具够用：查状态、查能力、读记忆、算能耗、提动作。
# 注意：没有任何"直接执行高风险设备"的工具，这类通道根本不存在。
TOOLS = [read_device_state, list_capability, search_memory, save_memory,
         calc_energy_cost, propose_action]


def build_home_agent():
    return create_react_agent(
        model=get_llm(temperature=0),
        tools=TOOLS,
        prompt=SYSTEM_PROMPT,
    )


agent = build_home_agent()


async def stream_reply(question: str, thread_id: str):
    """SSE 流式输出；thread_id 隔离会话记忆。"""
    async for event in agent.astream(
        {"messages": [("user", question)]},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="messages",
    ):
        yield event
```

### 13.6 system prompt 里的安全边界（节选）

```text
你是家庭智能管家。规则：
1. 涉及设备控制前，必须先调用 list_capability 确认设备支持该能力与取值；
2. 门锁、燃气阀、电气总闸、布防撤防属于高风险操作，只能调用 propose_action
   生成待确认建议，禁止声称"已执行"；
3. 查询设备现状时，若返回数据标记为过期（is_stale），必须明确告知用户数据不新鲜；
4. 涉及健康或老人异常时，只能表述为"待确认线索"，不得作出医疗判断；
5. 引用用户偏好时，说明这条记忆来自哪里；不确定时直接说不确定。
```

## 14. 评测与验收

### 14.1 功能验收场景

- 查询某房间温度时，返回值、单位、时间戳、在线状态与来源；无数据时明确说明；
- 生成"看电影模式"时，先查能力档案再做冲突检测，能复现被剔除的动作及原因；
- 问及设备用法时，返回说明书原文段落、版本与适用型号；
- 请求开灯时正常执行；请求开锁时**只生成待确认建议**，界面不出现"已执行"；
- 查询未确认的房间/设备时，不编造设备，转人工确认；
- 能耗建议能追溯到输入曲线、电价时段与计算口径；
- 主动建议在免打扰时段不推送；关闭开关后不再产生普通级建议；
- 数据过期、缺失、设备离线或模型服务错误时，界面显示对应状态且不伪装成功。

### 14.2 建议的量化指标（试点后定目标）

| 指标 | 目标 | 怎么测 |
|---|---|---|
| 设备查询正确率 | ≥ 95% | 用例期望值与实际返回值比对 |
| 场景生成可用率 | ≥ 85% | 生成的动作序列能通过能力校验且无冲突 |
| 记忆引用准确率 | ≥ 90% | 回答引用的记忆确实存在且来源正确 |
| 高风险拦截率 | **100%** | 所有高风险请求必须只生成待确认项 |
| 数据过期识别率 | ≥ 95% | 过期/缺失/离线数据被正确标注 |
| 打扰投诉率 | 越低越好 | 免打扰时段推送次数 / 建议总数 |

**高风险拦截率是硬指标，99% 都不行** —— 一个能自己开门锁的助手没人敢让家人住进去。

## 15. 风险清单

| 风险 | 表现 | 对策 |
|---|---|---|
| 模型臆造设备功能 | 让模型打开不存在的"加湿功能" | 控制前强制调用 list_capability；工具校验返回值 |
| 高风险动作被绕过 | 模型声称"已开锁" | 后端四层护栏 + 工具层不存在执行通道 + 审计 |
| 语音误唤醒误操作 | 电视里说了句"关灯"就执行 | 高风险需二次确认；低风险可加唤醒确认词与房间限定 |
| 多成员偏好冲突 | 空调温度各说各话 | 角色优先级 + 场景规则 + 记录冲突说明 |
| 传感器误报 | 燃气/水浸误告警导致恐慌 | 分级告警 + 多源交叉验证 + 标注置信度 |
| 记忆污染 | 一次误操作被记成"偏好" | 记忆带 source 与 confidence；低置信不参与个性化；可人工修正删除 |
| MQTT 消息乱序/重复 | 指令执行结果与实际不符 | 消息带时间戳与序号；指令去重；状态以设备回报为准 |
| 断网导致场景失效 | 云端不可用时全屋瘫痪 | 关键场景本地规则化执行（边缘网关承载） |
| 隐私顾虑 | 摄像头/麦克风被质疑 | 摄像头默认关闭或仅本地处理；物理开关说明可视化 |
| 数据过期仍被当实时 | 拿一小时前的温度做决策 | `is_stale` 标记 + 前端显式提示 + 不参与关键判断 |
| Docker：backend 先于 MySQL 启动 | 容器重启循环 | `depends_on: condition: service_healthy` + healthcheck |
| 容器里写了 127.0.0.1 | 连不上 MySQL/MQTT | 主机名必须是 Compose 服务名 `mysql` / `mqtt` |
| 时序查询过重 | 一次"全屋本月每秒功率"拖垮库 | 时间窗限制 + 连接池 + 限流 + 降采样 |
| Agent 陷入工具死循环 | 反复查同一个设备 | ModelCallLimitMiddleware + 最大步数上限 |

## 16. 角色、权限与确认矩阵

| 角色 | 可查看 | 可操作 | 不具备的权限 |
|---|---|---|---|
| 户主 | 全部房间、设备、告警、报告 | 配置场景、成员权限、确认高风险动作 | 绕过二次确认 |
| 老人 | 授权房间与设备 | 语音/简单操作、紧急呼叫 | 修改系统配置 |
| 儿童 | 受限设备与内容 | 学习模式、受限设备开关 | 接触高风险设备 |
| 访客/保姆 | 临时授权区域 | 临时门禁、局部设备 | 查看历史记录与成员档案 |
| 运维/售后 | 设备档案、故障记录 | 诊断、创建工单 | 查看家庭隐私数据 |
| 管理员/审计员 | 系统状态与审计日志 | 账号、备份、日志管理 | 查看无授权的家庭内容 |

高风险建议至少经过「提出—复核—确认—执行反馈」四个状态，平台只记录家庭审批，**不把审批动作自动转成设备指令**。

## 17. 非功能与安全要求（集中条款）

| 类别 | 要求 |
|---|---|
| 安全边界 | 首期为**家庭辅助平台**，高风险设备不自动执行；任何自动化控制扩展须单独评估与现场验收 |
| 可靠性 | 断网时基础场景由边缘网关本地执行；云端/模型不可用时监控与手动控制仍可用 |
| 隐私 | 本地优先处理；摄像头/麦克风默认关闭或仅本地；数据加密；家庭成员有权查看与删除自己的记忆 |
| 可解释性 | 输出区分设备实测、计算结果、模型推测、记忆引用与人工确认 |
| 时效性 | 刷新周期与告警响应按设备能力配置，禁止虚构实时性 |
| 可维护性 | 设备档案、房间拓扑、阈值、免打扰规则由授权成员维护并留变更记录 |
| 无障碍 | 老人语音、大字号、方言与多语言支持 |
| 成本 | 控制云端模型调用频次；能耗优化需真实数据支撑，不夸大省费数字 |

## 18. 数据责任与质量方案

| 数据对象 | 责任方 | 平台责任 | 发布前确认 |
|---|---|---|---|
| 设备档案、房间拓扑 | 户主 | 维护版本与生效时间 | 户主 |
| 设备遥测与质量标记 | 设备/网关 | 校验格式、标记离线与过期 | 系统自动化 |
| 成员偏好与记忆 | 各成员本人 | 保存来源、置信度与可删除入口 | 成员本人 |
| 设备说明书与安防预案 | 管理员 | 解析、版本过滤与引用 | 管理员 |
| 主动建议与打扰规则 | 户主 | 保存建议、原因、等级与反馈 | 户主 |

数据质量规则：

- 每条遥测带 `observed_at`、单位、来源、质量码与是否过期标记；
- 导入数据先校验字段、单位、时间顺序、重复记录与设备编码，失败记录进待处理清单；
- 过期、缺失、越界或质量异常的数据参与分析时必须显式提示，**不能用模型补值**；
- 设备档案与阈值采用版本化发布，变更前后保留确认人与生效时间；
- 报告保存输入时间窗、数据版本、计算口径与生成时间，便于复盘。

## 19. 里程碑排期

建议按 **6 周 MVP** 估算；每个里程碑结束都有能点起来的东西。

| 里程碑 | 内容 | 交付物 | 周期 |
|---|---|---|---|
| **M0 仓库骨架** | backend / frontend / gateway 三个子工程 + Compose + .env；前后端本地能启动 | 空壳可启动 | 第 1 周 |
| **M1 数据底座** | MySQL 初始化 SQL（含只读账号）+ 设备档案 + MQTT 虚拟设备脚本；跑通 `print(db.get_table_info())` 确认 COMMENT 生效 | 30 台虚拟设备能上报、写操作被拒 | 第 1 周 |
| **M2 态势与 RAG** | 房间卡片、设备列表、趋势；说明书入库 + 检索工具 | 全屋态势可见、能查说明书 | 第 2 周 |
| **M3 场景编排** | 自然语言 → 场景定义 → 能力映射 → 冲突检测 → 执行（含失败重试降级） | "看电影模式"可生成可执行 | 第 2–3 周 |
| **M4 Agent + API** | 协调 Agent + 5 专业 Agent 骨架；高风险确认节点；FastAPI 全部接口 + SSE | curl 能问、能流式、混合题可多步 | 第 3–4 周 |
| **M5 前端** | Vue 页面：总览/设备/对话框/场景/能耗/安防/建议中心；SSE 增量渲染 + 中断 | 浏览器里能正常使用 | 第 4–5 周 |
| **M6 能耗与照护** | 能耗曲线、账单对比、建议卡片；照护时间轴与异常预警 | 两大业务页可用 | 第 5 周 |
| **M7 联调与评测** | Docker 联调、20+ 用例批量跑、调 prompt / 阈值 / 切分 | 成功率与失败清单 | 第 6 周 |

**可以并行的部分**：M5 前端只要先按 10.3 约定好接口契约，就能与 M3/M4 完全并行——这也是契约要先写进 README 的原因。

**最容易卡的一步是 M3/M4**（能力映射与高风险确认链路），建议单独留时间，别指望两小时搞定。

**异步改造的落点**（别拖到 M7 上线前才补，那时候接口签名、调用链要整条返工）：

- **M2** 灌说明书时就用**批量 embedding**；
- **M4** 建 FastAPI 时一次性把接口写成 `async def`，连同 12.5 的异步工具、12.6 的连接池与限流一起做掉。

## 20. 量化验收目标（首期建议值）

以下目标需使用虚拟设备与模拟数据基准集确认后冻结：

- 设备与场景查询：已确认对象的名称、房间、来源展示正确率 ≥ 99%；
- 数据查询：正常样例的数值、单位、时间戳与在线状态展示正确率 ≥ 99%；
- 说明书引用：引用的文档版本与章节可定位率 ≥ 95%；
- 数据质量：对缺失、过期、离线数据的识别率 ≥ 95%；
- 风险边界：对门锁、燃气、布防撤防等请求**全部只生成待确认项**，生成审核提示率 100%；
- 报告完整性：能耗/日报草稿必填字段覆盖率 ≥ 95%，每项指标可追溯到输入记录；
- 可用性：核心看板与对话服务在试点期间达到双方约定的可用率，数值写入验收单。

指标不满足时，先区分数据质量、规则配置、检索召回、模型表达与前端展示原因，再决定修复或调整目标。

## 21. 实施偏差记录

本文件为规划稿，尚未对应真实实现。开工后在此记录规划与实际代码之间的差异：

| 位置 | 原规划 | 实际调整 | 原因/批准人 | 日期 |
|---|---|---|---|---|
| v1.0 | 初版规划 | 已写入本文 | 规划起草 | 2026-10-13 |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

## 22. 下一步

规划到这里，可以立刻开工的三件事：

1. **M0 + M1 脚手架一把梭**：仓库骨架 + docker-compose.yml（含 MQTT）+ MySQL 初始化 SQL + 只读护栏 + 30 台虚拟设备脚本。做完就能 `docker compose up` 看到全屋设备在跳动——这是整个项目最值得先做实的一块；
2. **M3 + M4 一条龙**：Agent 组装 + 能力校验 + 高风险确认链路 + FastAPI 接口 + Vue 对话页（含 SSE）。做完就是一个能在浏览器里和家对话的完整闭环；
3. **M5/M6 业务页**：场景编排页 + 能耗看板 + 建议中心，撑起答辩演示的"好看"部分。

另外两件可以顺手做的：

- 把需求分析里 agent1~agent5 的五个演示页先做成静态 HTML，作为真实前端的原型；
- 把设备虚拟能力档案整理成 YAML，方便后续换成真实 Matter / Home Assistant 设备时只改配置。

## 附：章节索引

| 章节 | 内容 | 什么时候看 |
|---|---|---|
| 1 / 1.1 | 定位与四层架构对齐 | 开工前建立整体印象 |
| 2 | 为什么是 Agent + 长期记忆 | 想解释"为什么不用规则引擎" |
| 3 | 场景设定与首期范围 | 确认边界、防止范围蔓延 |
| 4 | 需求到模块映射 + Agent 职责 | 想知道某段代码为什么这么写 |
| 7 | 数据库设计 | M1 |
| 8 | 目录结构 | M0 |
| 9 | 八个关键设计决策 | **最重要的一节**，尤其是第 1、2 条 |
| 10 | 前端设计 + 接口契约 | M5 |
| 11 | Docker 部署 + 上线清单 | M7 |
| 12 | 异步架构 | 并发出问题时先来这儿 |
| 13 | 关键代码骨架 | 写代码时照着抄 |
| 14 / 20 | 评测与量化验收 | M7 |
| 15 | 风险清单 | 卡住的时候先来这儿翻 |
| 16 / 17 | 权限矩阵与非功能要求 | 安全评审时 |
| 19 | 里程碑排期 | 开工前 / 卡住时对齐进度 |
| 21 | 实施偏差记录 | 发现文档和代码对不上时 |
