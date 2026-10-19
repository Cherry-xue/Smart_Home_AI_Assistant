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
- **没有列就塞 JSON，不要想着 ALTER TABLE**：`app_rw` 无 DDL 权限。已用此法落地的有
  场景收藏 `scenes.definition_json["favorite"]`、户型几何 `config/floorplans/{home_id}.json`。
  ⚠️ JSON 列改完必须**整体重新赋值**（`obj.json_col = {**old, ...}`），原地改 dict 不会生成 UPDATE
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
- **告警/照护/建议是「读时推导」**：三个列表接口默认先跑一次幂等 `sync_*` 再查（`?sync=false` 可关）。
  幂等靠业务键而非 event_id —— `alarms`/`suggestions` 没有该列，`app_rw` 无 DDL 权限：
  告警 = `(alarm_code, device_id, created_at±1s)`；建议 = `reason_json.dedupe`
- **照护必须用 `activity_level` 分睡/醒**（阈值 20）。只看 `presence` 会把整夜在床记成"夜间活动"
- 生成器场景：自动间隔 2.5~5.5 模拟小时（`auto_repeat`），演示用
  `python scripts/trigger_scenario.py gas_leak` 经 `smart_home/{home}/system/scenario` 手动点亮
- ⚠️ **补历史必须在后端刚启动时跑**。若常驻生成器已把时间线推到"现在"再补 24h，
  采集端全判「时间倒退」，能耗积分对倒退不计 → 电量恒 0。修复脚本 `scripts/repair_energy.py`
- ⚠️ Vue 模板 `v-if="空数组"` 为真值，必须写 `.length`
- ⚠️ **多套房红线：任何"会挑设备的代码"都必须先按 `home_id` 收窄目录**
  `DeviceCatalog.for_home(home_id)`（**None = 不过滤**，兼容旧调用方）。
  收窄在**目录层**做，不要给每个查询加 home_id 参数 —— 编排链路 nl/planner/executor
  全程只认 catalog，改一处就全生效，也不会漏掉某一处（漏一处 = "在 2 号房点按钮，
  动作落在 1 号房设备上"）。已落地：`/scenes/preview|plan`（请求 home_id）、
  `/scenes` 保存（body.home_id）、`/scenes/{id}` 改与执行（scene.home_id）、
  agent 的 plan / execute / settle_high_risk 三个节点（state/ctx 的 home_id）。
  ⚠️ **每次进入节点现算，绝不在建图/闭包外缓存**：`ctx.catalog` 会被 `refresh()`
  就地更新，而图是按 `(id(catalog), home_id)` 进程级缓存的，提前算好会永远停在旧快照
  （新加的设备永远进不来）。反例：`app.state.catalog` 必须是全量目录，
  采集器/质量评估器靠它认所有套房的设备
- ⚠️ 前端拼时间查询参数**禁止 `toISOString().slice(0,19)`**（发 UTC，后端按本地解析 → 整体偏 8h，短窗口直接查空）；要按本地字段手拼
- ⚠️ 重启后端**按 PID 杀**（`Get-NetTCPConnection -LocalPort 8000`），绝不用 `taskkill //F //IM python.exe`——会把常驻模拟器一起带走；恢复顺序 后端 → 模拟器
- **户型图几何存 `config/floorplan.json`（不存库）**：`rooms` 无几何字段 + `app_rw` 无 DDL 权限。
  底图存 `config/floorplan_assets/base.png`；房间码保存时必须过 `rooms` 表校验（进 MQTT topic），
  只有 `is_circulation=true` 的交通空间可以不在库中，且真实房间不许标成交通空间。
  上传底图会清空旧框选（坐标系变了）；有底图时画布尺寸必须等于图片尺寸；
  有底图时不画墙线、房间名与读数移到左上角加白描边（否则与图内房间名叠成糊字）
- ⚠️ 自动化画框时 `setPointerCapture` 对合成 pointerId 抛错 → 组件里要 try/catch
- ⚠️ **拖拽画框不能只靠 pointermove 累积尺寸**：快速拖拽时浏览器可能一个 move 都不派发，
  `draft` 会停在 0×0 → 表现成"画多大都提示框太小"。必须在 `pointerup` 用**抬手坐标**兜底，
  并把 pointerup 也挂到 window（画布外松手时状态才不会卡住）。
- ⚠️ **"框太小"还剩两个坑**：① 单击画布（零位移）也会走 onUp，必须静默忽略（位移 < 2）；
  ② SVG 里的 `<image>` 会被浏览器当成可拖拽图片，拖到一半发 `pointercancel` → 尺寸 0×0。
  解法：底图 `pointer-events="none"` + 单独一层兜底命中 `<rect>` + `pointerdown` 里 `preventDefault()`，
  且 `pointercancel` 一律静默中止、不弹提示。
- ⚠️ **Vue `<Transition>` 的过渡类加在页面根节点**。`.page-enter-from .page-fade`（有空格）
  永远匹配不到根节点自己，必须同时写 `.page-enter-from.page-fade`；否则这些页面"没有动画"却不是报错。
- **房间命名只在户型编辑器里做**：新建套房 **一个房间都不建**（`DEFAULT_ROOM_NAMES = ()`，
  `_clean_rooms` 允许 0 房间），房间全靠画框 + 写名字时创建，加房间走 `POST /api/homes/{id}/rooms`。
  ⚠️ 别再加回"新建时填房名"或"自动建占位房间" —— 占位没画框就变成幽灵房间，
  只能让用户手动「移除」，是纯噪音。
- **多套房**：`home_id` 贯穿所有取数。户型按套房存 `config/floorplans/{home_id}.json`，
  只有 1 号房有内置默认户型（别的套房没画过 = `source="empty"` 空画布）。
  `/api/devices` 与 `/api/energy` 的 `home_id` **缺省 1 而不是 None**（旧大屏不传，缺省 None 会串台）。
  生成器必须按 `device.home_id` 发布 topic，订阅用 `smart_home/+/...`，场景主题判 `endswith`。
  `/api/health` 的设备数是全库口径，页面统计要按当前套房自己数
- **房间名唯一真相是 `rooms.name`**：户型编辑器改名时写回数据库并 `catalog.refresh()`，
  交通空间名只存在几何文件里。房间码仍必须是 `rooms.code`（进 MQTT topic）
- ⚠️ **户型编辑器有 `baseBox` 与 `viewBox` 两个范围**：`baseBox` = 画布本体（**保存写它**），
  `viewBox` = 当前视口（缩放/平移只改它）。保存误用 viewBox 会把画布裁成放大后的那一块。
  底图 `<image>`、命中 `<rect>`、拖框边界都用 baseBox；手柄尺寸用 `HANDLE / zoom` 保持视觉恒定。
  平移是**右键拖动**（`button === 2` + svg 上 `@contextmenu.prevent`）；框/手柄的 pointerdown
  不能加 `.stop`，要在 handler 里对 `button === 2` 早返回，事件才能冒泡到画布去平移。
- **底图只用于编辑器辅助绘图，总览页不显示**（`FloorPlan.vue` 里没有 `<image>`、
  也没有"有底图/无底图"两套渲染）：总览统一画墙线 + 房间名居中 + 读数在下。
  换底图清框（坐标系变了）；**只删底图不清框**（`DELETE /api/floorplan/image`）。
  ⚠️ 总览的行距必须用 `upx(屏幕像素)` 换算，不能写死用户单位 —— 画布 2358 宽时
  写死 9/12 会让 13px 的字叠成一团（实测过）。
- ⚠️ **设备 code 必须全库唯一**：`DeviceCatalog` 用 code 做 key，两套房各有一个
  `temp_sensor_1` 会让一台在目录里被覆盖（"某套房少一台设备"）。`_next_device_code()` 与
  新增设备的查重都**不能带 home_id 条件**。
- ⚠️ **自建房间（`room_N`）没有物理档案**：`physics.py` 必须给设备所在房间兜底补
  `room_temp` / `room_humidity`（`_ensure_room_climate`），否则 `KeyError` 会让**整个生成器崩掉**。
- **删房间连设备**：`DELETE /api/homes/{hid}/rooms/{code}?confirm=true`，按
  `device_child_tables()` 清 6 张设备子表 + 房间维度孤儿告警；编辑器里点行「删除」只是
  `pendingDeletes`，**保存时**才真删（先存几何再删，否则几何引用已删房间会 400）。
  删除最后一个框时允许 `areas: []`（空户型合法）。
- **设备指令回执要轮询**：`DeviceControls.refreshLast()` 最多 8×1.5s，超时明确提示
  "检查模拟器/MQTT"。只查一次会出现"发了没反应"的假象。
- 枚举控件用 `el-select`；`format.js` 的 `ENUM_TEXT` 补了 `mode` / `fan_speed` 中文。
- **房间名唯一（走廊除外）**：两个卫生间要叫「主卫/次卫」。重名时前端红框 + 拦截保存，
  后端 `validate_payload` 也要查（并且把"没画到图上的房间"算进来）。
- **户型编辑器选的是「房间类型」不是具体房间**：类型是自由文本（allow-create），
  只存在户型 JSON 的 `areas[].type`，不进数据库；名字仍由用户输入。
  类型清单：客厅/卧室/厨房/卫生间/阳台/餐厅/书房/玄关/储物间/其他房间/走廊。
- **走廊可以有任意多段、可重名**：`走廊/通道/过道` 都算交通空间，每段一个 `hallway_N`
  编码（不进 `rooms` 表）。
- **`missing_rooms`（图上没有的房间）**：`rooms` 表有、`areas` 无对应 code 的房间。
  现在新建套房不带任何房间，正常情况不会有幽灵房间；只在"手动建过房间但没画框"时出现。
  编辑器里每个都带「移除」（`DELETE /api/homes/{hid}/rooms/{code}`），否则这种房间**没有别的删除入口**。
  前端算"已画出"时必须**同时按 code 和名字**匹配，且排除 `pendingDeletes`，否则会把
  "刚打了名字还没保存"和"刚删了框"算成没画出来。
- ⚠️ **测试绝不能碰真实套房**：只在临时套房里写数据；写操作前先确认 `home_id`。
  2026-10-19 因为把用户正在用的 `次卧1` 当成测试残留，`reset` 掉了 1 号房户型（框没了）。
- **套房的增删**：`POST /api/homes`（房间名用户定，**编码后端生成 `room_1…`**）、
  `DELETE /api/homes/{id}?confirm=true`（不带 confirm 一律 400；禁止删最后一套）。
  级联删除**必须批量** `WHERE device_id IN (SELECT …)`，逐台循环删 300 万行会卡几分钟。
  外键全 RESTRICT，顺序：读数/事件/指令/能耗/能力 → 告警 → 场景运行 → 照护/偏好 →
  用户角色 → 用户 → 建议/记忆/对话/规则 → 场景 → 成员 → 设备 → 房间 → 套房
- **`add_device` 的 home_id 从房间推导**，别再写死 1（写死会导致新套房加不进设备）
- ⚠️ **删除大事务期间不要 kill 后端**：MySQL 回滚要几分钟并锁表，其余写入报
  `1205 Lock wait timeout`；`app_rw` 无 PROCESS 权限查不了 `INNODB_TRX`
- ⚠️ **批量发版式的脚本改代码，替换串必须逐字核对**。上一轮给页面插
  `useHomeStore` import 时匹配串少了个 `ElMessageBox`，`str.replace` 静默失败、
  `useHomeStore()` 却插进去了 → 设备页白屏（构建还不报错）

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
- **页面骨架与切页动效已统一**（知识库 / 场景编排先行，后续页照做）：
  左菜单 + 中间主区（对话流 + 底部输入条）+ 右栏常驻信息。
  动效只用 `styles/transitions.css` 的三个全局钩子 —— `.page-fade`（淡入淡出）、
  `.page-slide`（从下升起 / 向下滑出）、`.page-slide-right`（从右滑入 / 向右滑出）；
  ⚠️ 时长必须 ≤ 外壳 `:duration="{enter:400, leave:280}"`，超了会被 Vue 截断

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
| M6 安防/照护/建议 | ✅ 后端 + 2 页前端 | `m6_check` 63 PASS；告警带判据/证据，照护分「在床/活动」，建议带免打扰降级 |
| M6 能耗页 | ✅ 前端页面 | 分时电价/逐小时/高耗设备，金额由 `core/tariff.py` 确定性算 |
| M7 评测 + Docker | ⏳ | Docker 未做；批量用例表未做 |

**启动**：`python scripts/start_dev.py --backfill`（一键）或手动三步（mosquitto → 后端 → 生成器）；
大屏 `cd frontend/dashboard && npm run dev`。详见项目根 `README.md`。
⚠️ **常驻模拟器不可省**——没有它 MQTT 上没人收 `cmd`，指令静默失效（最容易被误判成 bug）。
