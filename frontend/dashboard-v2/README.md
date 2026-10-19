# 新版 PC 大屏（dashboard-v2）

旧大屏在 `frontend/dashboard/`（端口 **5173**），本工程是**新建**的，端口 **5174**，
两套可以同时开着做对照。它们指向同一个后端（8000）。

## 启停

```bash
# 后端要先起来（项目根）
python scripts/start_dev.py

# 本工程
cd frontend/dashboard-v2
npm install --registry=https://registry.npmmirror.com
npm run dev            # → http://127.0.0.1:5174
```

端口默认 5174，可用 `VITE_PORT=5175 npm run dev` 覆盖。
⚠️ `strictPort: true` —— 端口被占会**直接报错**，不会偷偷换一个（否则你以为打开了新前端，
其实打开的是旧的）。

## 当前进度

只有**「房屋总览」**是实现的，其余 6 个菜单项是占位页（占位页里列了各自可用的后端接口）。

房屋总览的交互：
- **鼠标悬停**房间 → 该房间轻微放大（1.05）
- **点击**房间 → 该房间放大（1.22），其余房间模糊+淡出；
  左侧出现「详细数据」、右侧出现「器件开关」
- 再点一次同一房间 / 点空白处 → 取消选中
- 没有可显示数据的房间 → **隐藏左侧面板**
- 没有可控器件的房间 → **隐藏右侧面板**

## 目录

```
src/
  styles/tokens.css       ⚠️ 全项目只有这里允许写死颜色/尺寸，换配色只改这个文件
  api/http.js             fetch 封装（含 SSE），从旧大屏迁移
  api/index.js            接口清单 —— 字段名只在这里定义一处
  utils/format.js         空值一律 '—'、指标英文码→中文名、设备类型→中文名
  stores/system.js        /api/health + 待确认（顶栏红点、左下角数据时间共用）
  layout/AppShell.vue     顶边栏 + 主菜单 + 数据时间/刷新 + 消息抽屉
  router/index.js         7 个菜单项与路由一一对应
  pages/overview/
    roomGeometry.js       ⚠️ 户型坐标只在这里改
    FloorPlan.vue         户型图（悬停/选中/模糊）
    RoomDataPanel.vue     左侧「详细数据」
    RoomSwitchPanel.vue   右侧「器件开关」
    index.vue             拼装 + 数据拉取
```

## 已知未接入项（别误判成 bug）

1. **器件开关的后端端点不存在**。后端目前只有 `/api/chat` 和 `/api/scenes/{id}/run`
   两条控制通道，没有 `POST /api/devices/{id}/command`（`app/tools/` 的工具层是严格只读的，
   还有 `_assert_no_execution_channel()` 断言拦着）。所以右侧开关第一次点击会报
   「未接入」并把整块置为只读 —— 这是**如实反馈**，不是故障。后端补上端点即可直接用。
2. 界面**不做乐观更新**：点开关后设备状态要等下一轮轮询才变，
   措辞也统一是"已下发"而不是"已开启"（只有真改了值才能说改了值）。

## 硬规矩（从旧大屏继承，别违反）

- 空值一律渲染 `—`，**绝不用 0 冒充**
- 接口字段只在 `src/api/` 定义一处
- 顶栏/左下角显示**数据锚点**而不是真实时间；模拟数据必须有角标
- 高风险设备（门锁/燃气阀/总闸）**永不自动执行**，只生成待确认项
