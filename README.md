# 智能家居 AI 助手

FastAPI + MySQL + LangGraph 多 Agent 的智能家居助手，设备数据由虚拟设备运行时产生
（**没有真实硬件**，所有数据来自模拟器，界面上必须显式标注来源）。

---

## 一、启动（每天开机做一次）

### 方式 A：一键启动（推荐）

**双击项目根目录的 `start.cmd`** 即可（等价于下面这条命令）：

```bash
# 首次 / 数据太久没更新：补 24 小时历史，然后常驻
python scripts/start_dev.py --backfill --web

# 日常：直接常驻（不补历史）
python scripts/start_dev.py --web

# 只想跑后端那套（不拉大屏）
python scripts/start_dev.py --no-web
```

它会**按正确顺序**拉起四样东西（顺序不能乱），缺哪样补哪样：

```
MQTT Broker（1883） → 后端 API（8000） → 虚拟设备运行时（常驻） → 大屏（5174）
```

大屏起来后会自动打开浏览器（`--no-open` 可关）。`start.cmd` 也支持传参：

```bat
start.cmd --web          :: 不补历史，快速启动
start.cmd --no-web       :: 只跑后端
```

**起不来时不用自己去翻日志**：脚本会直接把对应日志（`api.log` / `broker.log` / `web.log`）
的最后十几行打在上面的窗口里，包括完整的 Python 报错。

### 停止：双击 `stop.cmd`（或 `python scripts/stop_dev.py`）

```bash
python scripts/stop_dev.py            # 全停：Broker + 后端 + 模拟器 + 大屏
python scripts/stop_dev.py --dry-run  # 只看会杀哪些进程，不动手
```

> 不能只按端口杀 —— **虚拟设备运行时没有端口**（它只发 MQTT），
> 只关端口会漏掉它，下次启动就变成两个模拟器同时发数据、时间线错乱。
> 所以脚本是「按端口 + 按命令行关键字」两路一起收，再用 `taskkill /T` 杀整棵进程树
> （`terminate()` 只杀父进程，vite / node 会留下来占端口）。

如果你是在 `start_dev.py` 那个终端里，按 **Ctrl+C** 效果一样。

### 方式 B：手动三步

```bash
# 1) Broker（Mosquitto 不是 Windows 服务，每次开机都要手动起）
"D:/soft/anaconda3/envs/ai0707/Library/sbin/mosquitto.exe" -c "C:/ProgramData/mosquitto/mosquitto.conf" -v

# 2) 后端（必须等 Broker 起来之后再起，否则订阅连不上）
cd backend && python run_api.py --no-reload --port 8000

# 3) 虚拟设备运行时 —— ❗最关键，没有它你发的指令没人接收
cd .. && python -m gateway.simulator.publisher --start auto --speed 60 --duration 0 --max-ahead 3
```

> 需要补历史数据时（比如隔了几天没跑），先跑一次：
> ```bash
> python -m gateway.simulator.publisher --start back:1440 --duration 1440 --speed 600
> ```
> `back:1440` = 从当前时刻往前 1440 分钟开始，配合相等的 `--duration`，
> 终点正好落在**现在**（不会跑到未来）。

---

## 二、为什么必须开"虚拟设备运行时"

| 缺了谁 | 你会看到的现象 |
|---|---|
| Broker | 后端订阅连不上；`health.mqtt.connected=false`，模拟器启动即退出 |
| 后端 | 页面 502；MQTT 数据没人落库 |
| **虚拟设备运行时** | **下发指令"成功"，但设备状态纹丝不动** ← 最容易被误判成 bug |

后端只负责**下发**指令，真正改变设备状态的是模拟器进程。它跑了批量历史就会退出，
之后必须有一个常驻进程持续接收 `cmd` 并按物理规律演化状态（开空调 → 温度慢慢降）。

---

## 三、常用参数（模拟器）

| 参数 | 说明 |
|---|---|
| `--start` | `auto`（从库里最新续跑）/ `now` / `back:<分钟>`（往前补历史）/ ISO 时间 |
| `--speed N` | 时间倍速。`60` = 1 真实秒跑 1 模拟分钟 |
| `--duration N` | 模拟总分钟数，`0` = 一直跑 |
| `--max-ahead N` | 常驻时模拟时间最多领先真实 N 分钟（防止界面出现"未来时间"） |
| `--scenario X` | `gas_leak` / `water_leak` / `smoke` / `no_activity` / `night_wake` / `intrusion` |

**推荐组合**：`--start auto --speed 60 --duration 0 --max-ahead 3`
—— 60 倍速让状态变化看得见，同时时间轴最多领先真实时间 3 分钟。

---

## 四、验收脚本

```bash
cd backend
python tests/smoke_test.py      # 环境冒烟
python tests/schema_check.py    # ORM ↔ 数据库一致性
python tests/ingest_check.py    # MQTT 落库
python tests/api_check.py       # 态势 API
python tests/rag_check.py       # 说明书检索
python tests/scene_check.py     # 场景编排
python tests/agent_check.py     # 多 Agent 编排（默认离线，--api 加 API 层）
python tests/preset_check.py    # 预设设备 ↔ 数据库能力对账
python tests/m6_check.py        # M6：安防告警 / 照护观察 / 主动建议
python tests/floorplan_check.py # 户型图：几何校验 / 底图上传 / 重置 / 改名规则
python tests/homes_check.py      # 套房：新建 / 校验 / 级联删除（会真建一套再删掉）
python tests/web_check.py       # 前端契约对账（⚠️ 会大量调用真实模型，注意额度）
```

---

## 五、户型图自定义 / 多套房

户型几何默认来自前端 `roomGeometry.js` 里的内置户型（**只对 1 号房有效**）；
一旦自定义过，就改读后端 `config/floorplans/{home_id}.json`（**不存数据库**：
`rooms` 表没有几何字段，加字段要 DDL，而 `app_rw` 没有 DDL 权限）。

用法：**顶栏左上角「编辑」**（在「新增」「删除」中间，任何页面都能打开）

1. 「上传户型底图」选一张 PNG/JPEG/WEBP（≤8 MB）。后端会用 Pillow 真解码一次、
   清掉 EXIF、统一转 PNG，并**把画布尺寸设成图片尺寸**（这样框选坐标与图对齐）。
   传错了点旁边的 **「删除底图」**：只删图，框选/房间/画布尺寸都保留。
   ⚠️ **底图只在编辑器里显示**，是辅助画框的参照；房屋总览页不画底图。
2. 在图上拖拽框出房间，**松手后光标会自动落在该行的名字输入框**，直接打字命名。
   也可以点中框拖动位置、拖四角/四边改大小。
3. 下拉选的是**房间类型**（客厅/卧室/厨房/卫生间/阳台/餐厅/书房/玄关/储物间/其他房间/走廊），
   类型可自由输入，不做约束；选完会自动用它当名字（选「其他房间」则留空让你自己写）。
4. **房间名由你定，但不可重复**（走廊除外）：重名时输入框描红、保存会被拦下并提示
   "已有同名房间「X」"。打了新名字的框在「保存」时创建房间（编码后端生成）。
5. 走廊/过道可以画多段、可以同名，每段自动拿一个 `hallway_N` 编码（不进 rooms 表）。
   真正建库放在保存时，是为了让「取消」什么都不留下。
6. **点某一行的「删除」= 删掉这个房间**：确认框会写明「房间 + N 台设备 + 历史数据」，
   点「保存」后真正执行（`DELETE /api/homes/{id}/rooms/{code}`），不可恢复；
   只删了框没保存 / 反悔点「取消」，什么都不会删。删到最后一个框也允许保存（户型变空）。
7. **缩放看细节**：工具条「放大 / 缩小 / 还原」，也可以**滚轮**缩放、**右键拖动**平移
   （画布上右键不会弹浏览器菜单；右键落在框或手柄上也是平移，不会误拖框）。
   缩放只改视口（看哪一块），**保存写的仍是原始画布尺寸**，不会把户型裁小。
   画框/拖框始终吸附在真实画布内，缩小后四周的留白点不动。
8. 想重来：**「清空所有框」只清图上的框**（房间不删，底部会列出"图上没有的房间"并可逐个移除）；
   **「恢复默认」连底图一起删掉**。

多套房：顶栏左上角的下拉可以切换套房（选择记在 localStorage）。
每套房有独立的房间、设备、户型图和能耗。

**建 / 删套房直接在界面上做**：切换器右边有「新增」「编辑」「删除」。

- 新建：**只填套房名**。新套房是空的 —— **不预建任何房间、也没有户型图**。
  建完点顶栏「编辑」，在画布上拖框 + 写名字，房间就按你写的名字创建
  （名字和位置在同一处说清，不会有"图上一个、列表里另一个"的幽灵房间）。
  **房间编码由后端生成**（`room_1`、`room_2`…），因为编码要进 MQTT topic，不能带 `/ + #`。
- 改名：顶栏「编辑」打开后，**标题行里就能改套房名称**（和房间名不是一回事）。
  保存时先改名再存户型，改名失败不会留下"户型存了、名字没改"的半成品。
- 删除：**会连同房间、设备、全部历史数据（读数/事件/告警/能耗/照护）和户型文件一起删**，
  不可恢复；确认框里会写出这台套房下的设备数与房间数。**最后一套房不允许删**。
  带几百万行数据的套房删除要等几秒到几十秒，期间请勿重复点击。

命令行建第二套演示房（含设备，适合演示/初始化）：

```bash
python scripts/seed_second_home.py          # 建「工作室单间」：2 房间 + 6 设备（幂等）
python scripts/seed_second_home.py --reset  # 重建（⚠️ 会删这套房的历史数据）
```

几条刻意的约束：

- **换底图会清空旧框选** —— 坐标系变了，保留旧框等于保留一堆错位的东西。
- 已有底图时，保存的画布尺寸必须与图片一致，否则 400（否则框和图对不上）。
- 漏画的房间会在保存提示里点名（"XX 没有画到图上，这些房间的设备不会显示"）。
- **交通空间（走廊）必须有名字**；真实房间的名字会写回数据库，不许只存在前端。
- 有底图时不再画墙线（墙在图里），房间名与读数移到左上角并加白描边 ——
  否则会和图片自带的房间名叠成一团糊字。
- `/api/devices` 与 `/api/energy` 的 `home_id` **缺省是 1**：不传参数的旧调用方
  仍然只看到 1 号房，不会因为多了第二套房而串台。
- **新套房不继承任何户型**：`source="empty"` 画空画布，提示去「编辑户型」画自己的。
- 弹窗高度锁在 `min(90vh, 880px)`：画布用 flex 吃掉剩余高度，房间列表**固定 4 行高度**
  （`height` 而非 `max-height`，否则行数变化会让画布变矮、正在拖的框会跳）。
- **房间名唯一**（走廊除外）：两个卫生间要叫「主卫 / 次卫」，不能都叫「卫生间」。
  走廊是交通空间、不建房间记录，因此可以重名。
- **删房间会连设备一起删**：房间是设备的归属，留下设备会挂在"不存在的房间"上。
  删房间走 `DELETE /api/homes/{id}/rooms/{code}?confirm=true`，会清理设备的 6 张子表。
- 页面切换统一走淡入淡出（`styles/transitions.css`）：`page-fade` 既可以挂在页面根节点，
  也可以挂在根节点里的子区块上，两种都有对应规则。

---

## 六、目录

```
start.cmd    Windows 双击一键启动（= scripts/start_dev.py --backfill --web）
stop.cmd     Windows 双击一键停止（= scripts/stop_dev.py，--dry-run 可先看再杀）
backend/     FastAPI 后端（app/{api,agent,services,mqtt,models,db,tools}）
frontend/
  dashboard-v2/ PC 大屏（Vite + Vue3 + Element Plus + ECharts，纯 JS）
                ← 旧版 dashboard/ 已于 2026-10-19 移除（git 里仍可找回）
  mobile/        手机端（uni-app 经典版，HBuilderX 打开）← 待做
gateway/
  simulator/ 虚拟设备数据生成器（物理引擎 + 坏数据注入 + 场景）
  start_mqtt.cmd
scripts/     start_dev.py 一键启动 / stop_dev.py 一键停止 /
             trigger_scenario.py 手动触发异常场景 /
             seed_second_home.py 建第二套演示房 / repair_energy.py 修能耗增量
config/      tariff.yaml（电价）、scene_intents.yaml（场景约束）、
             device_presets.yaml（可添加的预设设备）、insights_rules.yaml（告警与照护判据）、
             floorplans/{home_id}.json + floorplans/{home_id}/base.png（户型几何与底图，默认不存在）
mysql/init/  建库 SQL（01~04 结构 + 种子，99 校验）
文档/         需求分析 / 规划书 / 技术选型方案
```

---

## 六、几条必须守住的约定

- **高风险设备**（门锁 / 燃气阀 / 总闸 / 布防撤防）：只生成待确认项，**永不自动执行**。
  工具层没有执行通道，`HIGH_RISK_AUTO_EXEC=false`。
- **不谎报**：下发成功 ≠ 设备状态已变。回答只说"已下发"，设备回执（cmd_ack）落库后才算确认。
- **空值显示 `—`**，不用 0 冒充；模拟数据必须标注来源，不能伪装成真实实测值。
- 连接地址**全部从环境变量读**，代码里不硬编码 `127.0.0.1`。
- npm 安装务必加 `--registry=https://registry.npmmirror.com`（默认源在本机只有 13 KB/s）。
- Mosquitto 是 C 程序，**不认中文路径**：项目里的 `gateway/mosquitto.conf` 只是版本化源文件，
  运行时用的是 `C:\ProgramData\mosquitto\mosquitto.conf`（改配置要同步两处）。
