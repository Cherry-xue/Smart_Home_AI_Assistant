/** 路由级数据预取。
 *
 * 要解决的问题
 * ------------
 * 路由过渡是 `out-in`：旧页面先淡出 200ms，新页面才开始挂载、才发请求。
 * 于是"点菜单 → 看 200ms 旧页 → 看 300ms 空的新页（骨架/暂无数据）→ 数据到"。
 * 中间那 300~800ms 完全可以用来等数据。
 *
 * 做法：在**路由确认的那一刻**（`router.beforeEach`）就把目标页需要的接口发出去。
 * 配合 `api/http.js` 里 GET 的请求合并 + 2.5 秒缓存，
 * 新页面挂载后再请求同一批接口时要么复用还在飞的 Promise、要么直接命中缓存，
 * 首帧就是真实内容，不需要骨架。
 *
 * ⚠️ 这里的参数必须与页面里的调用**逐字一致**，否则缓存 key 对不上，预取白做。
 *    页面改了请求参数（比如多传一个 hours），这里要同步改。
 * ⚠️ 预取失败不能影响导航：所有 Promise 都吞掉异常，页面自己还会再请求一次。
 */

import {
  getDevices,
  getEnergySummary,
  getFloorplan,
  getKnowledgeStatus,
  getRooms,
  listAlarms,
  listCareObservations,
  listDocuments,
  listScenes,
  listSuggestions,
} from '@/api'

/** 路由名 → 该页首屏需要的请求。参数与页面里的调用保持一致。 */
const PREFETCH = {
  // 总览：户型几何（4ms，本地文件）+ 房间 + 设备 + 收藏场景 + 能耗汇总
  overview: (hid) => [
    getFloorplan(hid),
    getRooms(hid),
    getDevices({ home_id: hid }),
    listScenes(hid),
    getEnergySummary({ home_id: hid }),
  ],
  devices: (hid) => [getDevices({ home_id: hid }), getRooms(hid)],
  care: (hid) => [
    listAlarms({ home_id: hid }),
    listCareObservations({ home_id: hid }),
    listSuggestions({ home_id: hid }),
  ],
  energy: (hid) => [getEnergySummary({ home_id: hid })],
  scenes: (hid) => [listScenes(hid)],
  knowledge: () => [listDocuments(), getKnowledgeStatus()],
}

/**
 * 给即将进入的页面预热数据（fire-and-forget）。
 *
 * @param {string} name 路由名
 * @param {number} homeId 当前套房
 */
export function prefetchRoute(name, homeId) {
  const build = PREFETCH[name]
  if (!build) return

  let requests
  try {
    requests = build(homeId)
  } catch {
    return
  }

  for (const req of requests) {
    // 只关心"请求已发出"这件事，结果由页面自己消费；失败静默。
    if (req && typeof req.catch === 'function') req.catch(() => {})
  }
}
