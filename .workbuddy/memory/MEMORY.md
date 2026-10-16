# 智能家居 AI 助手 · 项目长期备忘（精简版，详细过程见各日日志）

> 本文件只保留**跨会话必须记住的结论与红线**；实现细节、踩坑全过程、验收数据都在
> `.workbuddy/memory/YYYY-MM-DD.md` 里。写新内容前先来这里对齐，别推翻已冻结的决定。

## 一、项目定位
课程/答辩项目，代号 AI0707。需求基线 `文档/智能家居AI助手项目需求分析.md`，
落地依据 `文档/智能家居AI助手项目规划书.md`，选型与全部实测记录 `文档/技术选型方案.md`（当前 v1.18）。
卖点：多模态感知 + 长期记忆 + 多 Agent 协作（1 协调 + 5 专业 Agent）+ 云边端协同。

## 二、已冻结的技术栈
- 环境：直接复用 conda `ai0707`（Python 3.13.15），**不新建环境**
- 后端：FastAPI + Uvicorn + Pydantic 2 + pydantic-settings；SQLAlchemy 2.0 **同步** + PyMySQL + `pool_pre_ping`
- 数据库：**MySQL 8.0.27 在虚拟机**（`192.168.159.100:3306`），本机不装库；Alembic 只出 diff，**DDL 在虚拟机执行，后端无 DDL 权限**
- Agent：**手写 `StateGraph`**（route → 5 专业 Agent → 高风险 interrupt → 汇总）；节点内用 `langchain.agents.create_agent` + middleware；HITL 用 **`interrupt()` + `Command(resume)`**
- 持久化：`langgraph-checkpoint-mysql[pymysql]` → **`PyMySQLSaver`**，独立库 `smart_home_checkpoint`
- 大模型：`ChatOpenAI` + DashScope compatible-mode，`model=qwen3.7-plus`，`temperature=0`
- 向量：`qwen3.7-text-embedding`（1024 维）；重排 `qwen3.7-text-rerank`（**不是** `qwen3-rerank`，且要走 dashscope 原生 SDK）
- 向量库：chromadb 嵌入式 PersistentClient（`chroma_db/`）+ `langchain-chroma`
- MQTT：Mosquitto 2.0.15（conda 装）+ 后端 aiomqtt + 生成器 paho
- 前端：**纯 JavaScript**（不用 TS）。`frontend/dashboard/`（Vite + Vue3 + Element Plus 深色 + ECharts）、
  `frontend/mobile/`（uni-app 经典版，HBuilderX `D:\Code\H build X\HBuilderX` 4.57）。**两个独立工程，只共享后端 API**
- 刻意不引入：Redis、Celery、langchain-community、langchain-classic、LlamaIndex；首期不启用 NL2SQL
- 部署本地直跑，Docker 产物延后

## 三、硬约束与红线（不许放松）
- 数据大概率无真实采集 → 来源必须可插拔（`DATA_MODE` = mock/mqtt/csv/real_gateway），上层无感
- **门锁 / 燃气阀 / 电气总闸 / 布防撤防：只生成待确认项，永不自动执行**（`HIGH_RISK_AUTO_EXEC=false`，工具层无执行通道）
- 数据不足不给确定性省费金额；健康异常只说"待确认线索"，不做医疗断言
- **只有真的改了值，才说自己改了值**——"已下发指令"≠"设备状态已变"
- 模拟数据必须显式标注来源/角标，不得伪装成真实实测值
- 所有连接地址从环境变量读，**代码里禁止硬编码 `127.0.0.1`**

## 四、接口契约（已冻结）
- topic：`smart_home/{home_id}/room/{room_code}/device/{device_code}/{state|telemetry|event|cmd|cmd_ack}` + `smart_home/{home_id}/system/heartbeat`
- 信封：`{device_code, observed_at, seq, source, quality, metrics, units}`；`seq` 用于丢包/乱序/重复检测
- 每条数据必带 `observed_at` / `source` / `quality` / `is_stale`
- 时间语义三分（极易混）：`observed_at` 设备观测时间 / `received_at` 本机收到时间（**判在线用它**）/ `last_observed_at` 最新数据观测时间

## 五、技术事实（已实测/查文档，勿凭记忆推翻）
- LangGraph v1 **弃用 `create_react_agent`** → `from langchain.agents import create_agent`，`prompt`→`system_prompt`；`InterruptOnConfig` 在 `langchain.agents.middleware.human_in_the_loop`。规划书 §13.5 骨架已过时
- checkpoint 类名是 `PyMySQLSaver` / `AIOMySQLSaver`，**不是** `MySQLSaver`；首次必须 `.setup()`；手工传连接要 `autocommit=True`
- ⚠️ **aiomqtt 在 Windows 必须 SelectorEventLoop**（Proactor 的 `add_writer` 抛 `NotImplementedError`，报错伪装成 paho `__del__` 噪声）；uvicorn 在 Windows 默认 Proactor → 建循环前调 `core/asyncio_compat.ensure_selector_loop_on_windows()`
- ⚠️ **`Command(resume)` 会重跑被中断的节点** → 节点内写库必须幂等（本项目按 thread_id+device+capability 复用行）
- ⚠️ 状态里的 Pydantic 模型必须登记 `app/agent/checkpoint.py: ALLOWED_MSGPACK_MODULES`，加新模型要同步登记
- MQTT `state` 带 retain → 订阅瞬间回放的历史快照**不参与时间基准判定**
- 查库端点写 `def` 不写 `async def`（同步 SQLAlchemy 会卡事件循环）

## 六、LLM 报错码与排查顺序（血泪教训）
- `403 AllocationQuota.FreeTierOnly` = 该模型版本免费额度用完；**别名（`qwen-plus`/`-latest`/`qwen-flash`）一律 403**，只有具体版本号可用
- `429 limit_requests` = **QPM 限流**（多 Agent 一次对话连发 5~10 请求必撞）⭐这才是"多智能体被拒"的真因
- 思考型模型（qwen3.x）拒绝 object 形式 `tool_choice` → `extra_body={"enable_thinking": False}`（已做成 `settings.llm_disable_thinking`，只能用 extra_body，不能进 model_kwargs）
- 排查固定顺序：① 三处 key 指纹比对 ② 同刻多模型对照 ③ 兼容模式 vs 原生协议 ④ 别名 vs 版本号 ⑤ **单次/连发/并发对照**
- ⚠️ 验证模型的结论，**不能拿已被自己打死的版本测**；跑 `web_check.py`（186 项）会烧一个版本额度，跑前先探测目标模型

## 七、环境与账号
- 虚拟机 MySQL：`192.168.159.100:3306`，8.0.27，utf8mb4，`lower_case_table_names=0`；已建 `smart_home`（业务）+ `smart_home_checkpoint`
- 账号：`app_rw`（业务读写，无 DDL，`.env` 默认）、`agent_ro`（仅 SELECT，实测写被拒 1142）、`root`（仅建库/DDL）
- ⚠️ **密钥类校验一律 sha256 比对，不看回显**（本环境会对输出里的密钥做脱敏替换）；密钥用脚本从外部文件复制，不手抄
- ⚠️ 连接串密码里的 `@` 必须 `%40`；根治办法是 `sqlalchemy.URL.create()` 从分量构造
- Mosquitto 路径：broker 在 `Library\sbin\mosquitto.exe`（**不在 bin**），客户端在 `Library\bin\`
- ⚠️ **Mosquitto 不认中文路径** → 运行副本放 `C:\ProgramData\mosquitto\mosquitto.conf`，与项目内 `gateway/mosquitto.conf` **两处同步**
- Mosquitto **不是 Windows 服务**，每次开机手动起（`gateway/start_mqtt.cmd`）
- ⚠️ **npm 必须加 `--registry=https://registry.npmmirror.com`**（默认源 13 KB/s 装不动）
- 本机：Windows；RTX 3050 Ti 4GB（跑不动 7B）；Node 22/24；Git 2.55

## 八、前端约定（M5 起）
- 接口字段**只在 `src/api/` 定义一处**；空值一律 `—`（不用 0 冒充）
- 模拟数据必带角标，顶栏显示**数据锚点**而非真实时间
- SSE 用 `fetch` + `ReadableStream`（EventSource 不支持 POST）
- dev 走 Vite proxy 同源转发 `/api` → 后端默认不开 CORS
- ⚠️ `health.devices` 的键是 `total / rooms / high_risk / online`，**没有 `devices`**（写错静默显示 0）
- 大屏 7 页：态势总览 / 设备 / 能耗 / AI 对话 / 场景编排 / 知识库 / 系统状态

## 九、协作约定（用户明确要求）
- **未经许可不得擅自新增/改写代码骨架**，不要自作主张重写文件；用户会在 AI 回合之间自行编辑，需保留
- 技术结论必须基于实测或源码/官方文档核实，禁止想当然；bug 要按日志与源码定位根因，禁止猜测
- 沟通用简体中文口语化语气，称呼用户为"你"

## 十、进度
| 里程碑 | 状态 | 验收 |
|---|---|---|
| M0 环境/建库/账号 | ✅ | 冒烟 37 PASS；`agent_ro` 写入被拒实证 |
| M1 配置层 + ORM(24表) + 生成器 + MQTT 落库 | ✅ | `schema_check` 24 表/209 字段全一致；`ingest_check` 42 PASS |
| M2 FastAPI 态势 API + RAG 说明书检索 | ✅ | `api_check` 71 PASS；`rag_check` 30 PASS（8 题 Top-1 命中） |
| M3 场景编排 | ✅ | `scene_check` 44 PASS（"看电影却开主灯"被拦并给判据） |
| M4 多 Agent 编排 | ✅ | `agent_check` 59 PASS + 真实模型链路全通 |
| M5-1 Web 大屏 | ✅ | `npm run build` 通过；`web_check` 契约对账 186 项 |
| M5-2 手机端 uni-app | ⏳ 附加题，待开工 | — |
| M6/M7 端点（security/care/suggestions）+ Docker | ⏳ | — |

**启动**：`python scripts/start_dev.py --backfill`（一键）或手动三步（mosquitto → 后端 → 生成器）；
大屏 `cd frontend/dashboard && npm run dev`。详见项目根 `README.md`。
⚠️ **常驻模拟器不可省**——没有它 MQTT 上没人收 `cmd`，指令静默失效（最容易被误判成 bug）。
