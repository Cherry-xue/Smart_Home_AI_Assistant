# 智能家居 AI 助手 · PC 浏览器大屏（M5）

> 与手机端（uni-app）**完全独立**的两个工程：这个是纯 Web 大屏，跑在电脑浏览器上；
> 手机端在 `frontend/mobile/`，用 HBuilderX 打开。两者只共享后端 API，不共享代码。

## 技术栈

| 项 | 选择 | 理由 |
|---|---|---|
| 构建 | Vite 6 | 项目约定；dev 阶段用 `server.proxy` 同源转发 `/api`，后端不用开 CORS |
| 框架 | Vue 3（组合式 API）+ `<script setup>` | 项目约定 |
| 语言 | **纯 JavaScript**，不用 TypeScript | 项目约定（课程项目，降低阅读门槛） |
| UI | Element Plus（深色 `css-vars` 主题） | 表格/表单/抽屉开箱可用 |
| 图表 | ECharts 5 | 项目约定 |
| 状态 | Pinia | 只有"系统健康 + 待确认事项"需要跨页共享 |
| 路由 | vue-router 4 | 侧栏菜单由路由表生成，加页面只改一处 |

## 启动

```bash
# 1) 先起后端（另一个终端）
cd ../../backend
python run_api.py --no-reload --port 8000

# 2) 再起大屏
cd frontend/dashboard
npm install          # 国内网络慢的话加 --registry=https://registry.npmmirror.com
npm run dev          # http://127.0.0.1:5173
```

可选环境变量（写在 `.env.local` 或命令行前缀）：

- `VITE_API_TARGET` —— 后端地址，默认 `http://127.0.0.1:8000`
- `VITE_PORT` —— 大屏端口，默认 `5173`

想让数据在动，还要跑模拟器（否则设备全是离线）：

```bash
cd ../../backend && python -m gateway.simulator.publisher --speed 60 --duration 1440
```

## 页面

| 路由 | 页面 | 主要接口 |
|---|---|---|
| `/overview` | 态势总览：KPI + 房间卡片墙 + 用电趋势/排行 + 峰平谷 | `GET /homes/1/rooms`、`GET /energy/summary`、`GET /health` |
| `/devices` | 设备：筛选/表格/能力档案抽屉/历史曲线 | `GET /devices`、`GET /devices/{id}/capabilities`、`GET /devices/{id}/readings` |
| `/energy` | 能耗：时间窗、峰平谷表、逐小时电量+功率、设备排行 | `GET /energy/summary` |
| `/chat` | AI 对话：SSE 流式 + 高风险确认弹窗 + 待确认侧栏 | `POST /chat/stream`、`GET /confirmations/pending`、`POST /confirmations/{thread_id}` |
| `/scenes` | 场景编排：一句话 → 动作校验 → 剔除原因 → 保存执行 | `POST /scenes/preview`、`POST /scenes`、`POST /scenes/{id}/run` |
| `/knowledge` | 知识库检索：片段 + 文件/章节溯源 + 库状态 | `GET /knowledge/search`、`/documents`、`/status` |
| `/system` | 系统状态：数据库 / MQTT / 落库统计 / 缓存 / 电价 | `GET /health` |

## 三条不能破的规矩

1. **高风险设备永不自动执行。** 门锁 / 燃气阀 / 总闸只出现"待确认"，界面上
   每次弹窗都写明"批准 = 人工留痕，不等于系统执行"。侧栏底部常驻红线提示。
2. **没有值就是 `—`，不能用 0 冒充。** `utils/format.js` 里所有格式化函数在收到
   `null/undefined` 时返回 `—`。
3. **模拟数据必须显式标注。** 顶栏"数据时间"旁有 `模拟` 角标，`data_mode != real_gateway` 时出现；
   顶栏显示的锚点是**数据时间**，不是真实时间（模拟器按倍速跑，可能领先真实时间）。

## 目录

```
src/
├─ api/          接口封装（字段与 backend/app/schemas 一一对应，改契约只改这里）
├─ components/   EChart / StatCard / AnswerSections / InterventionCard
├─ layout/       DashboardLayout（侧栏 + 顶栏）
├─ router/       路由表（侧栏菜单来源）
├─ stores/       system.js（健康 + 待确认，15s / 10s 轮询）
├─ utils/        format.js（时间、数值、语义标签映射）
└─ views/        7 个页面
```

## 已知取舍

- ECharts 与 Element Plus **全量引入**：本地直跑，不差这点体积，省掉按需引入插件的配置复杂度。
- 图表内部颜色**写死**为深色：CSS 变量在 canvas 里读不到，不写死会画成黑底黑字。
- SSE 用 `fetch` + `ReadableStream`，不用 `EventSource`（后者只支持 GET、无法带请求体、无法用 `AbortController` 真停）。
