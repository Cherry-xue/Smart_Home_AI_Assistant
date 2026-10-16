# 智能家居 AI 助手

FastAPI + MySQL + LangGraph 多 Agent 的智能家居助手，设备数据由虚拟设备运行时产生
（**没有真实硬件**，所有数据来自模拟器，界面上必须显式标注来源）。

---

## 一、启动（每天开机做一次）

### 方式 A：一键启动（推荐）

```bash
# 首次 / 数据太久没更新：补 24 小时历史，然后常驻
python scripts/start_dev.py --backfill

# 日常：直接常驻（不补历史）
python scripts/start_dev.py
```

它会**按正确顺序**拉起三样东西（顺序不能乱）：

```
MQTT Broker（1883） → 后端 API（8000） → 虚拟设备运行时（常驻）
```

再**另开一个终端**起大屏：

```bash
cd frontend/dashboard
npm run dev          # → http://127.0.0.1:5173
```

停止：在运行 `start_dev.py` 的终端按 **Ctrl+C**，会一次停掉全部子进程。

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
python tests/web_check.py       # 前端契约对账（⚠️ 会大量调用真实模型，注意额度）
```

---

## 五、目录

```
backend/     FastAPI 后端（app/{api,agent,services,mqtt,models,db,tools}）
frontend/
  dashboard/ PC 大屏（Vite + Vue3 + Element Plus + ECharts，纯 JS）
  mobile/    手机端（uni-app 经典版，HBuilderX 打开）← 待做
gateway/
  simulator/ 虚拟设备数据生成器（物理引擎 + 坏数据注入 + 场景）
  start_mqtt.cmd
scripts/     start_dev.py 一键启动
config/      tariff.yaml（电价）、scene_intents.yaml（场景约束）
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
