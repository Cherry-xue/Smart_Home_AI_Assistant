/** 户型几何 —— ⚠️ **想调房间位置/大小，只改这个文件**。
 *
 * 坐标系就是 SVG 的 viewBox 坐标（左上角为原点，单位=用户单位）。
 * 按设计图 1 复刻：上面一排是公共区，中间一条**通道**，下面一排是休息区。
 *
 *      ┌────────┬──────────────────┐  ┌──────┬───────────┐
 *      │  阳台   │       客厅        │  │ 玄关  │   厨房     │   ← y 20..190
 *      ├────────┴──────────────────┴──┴──────┴───────────┤
 *      │              通  道（非房间，无设备）              │   ← y 190..232
 *      ├────────────┬───────────┬───────────────────────┤
 *      │   主卧      │  卫生间    │        次卧            │   ← y 232..385
 *
 * 三个刻意的设计：
 *
 * 1. **房间尺寸按设计图的比例来**（阳台118 / 客厅214 / 玄关105 / 厨房161；
 *    主卧168 / 卫生间106 / 次卧190），不是随手给的。
 *
 * 2. **通道是独立的一条**，`isCirculation: true`：它没有设备、不参与点选、
 *    不显示读数。用户指出过"连接卫生间/主卧/次卧的通道没画"，就是这条。
 *    它的左右两端（x=100 / x=564）和下排房间对齐，上边与客厅/玄关/厨房相通。
 *
 * 3. **门是"打洞"式实现**：不在数据里手写墙段，而是声明门的位置，
 *    由 `wallSegments()` 在生成墙线时把被门覆盖的那一段跳过。
 *    这样调整房间尺寸时，墙会自动重算，不会出现"改了坐标忘了改墙"。
 */

/** SVG 画布的 viewBox 分量（拆出来是为了算"画布中心"，选中房间要平移过去）。
 *
 *  ⚠️ 画布必须**刚好包住"所有房间 + 它们被放大平移后可能到的位置"**：
 *  选中房间会被搬到画布中心并按 1.22 放大，所以最大房间（客厅 214×170）
 *  在中心处占 261×207 —— 这就是留白量的下界，再小就会被裁掉一角。
 *  房间实际范围是 x:20~618 / y:20~385，内容中心 (319, 202.5)，
 *  所以取 x:5~633 / y:5~400 时，中心恰好等于 (319, 202.5)，四周留 15。
 *  （早先留了 60~80，画布偏高导致户型图整体偏小，横向大片留白。） */
export const VIEW_BOX = { x: 5, y: 5, w: 628, h: 395 }
export const PLAN_VIEWBOX = `${VIEW_BOX.x} ${VIEW_BOX.y} ${VIEW_BOX.w} ${VIEW_BOX.h}`
export const VIEW_W = VIEW_BOX.w
export const VIEW_H = VIEW_BOX.h

/** 画布中心。选中的房间会被平移到这个点上（"移到中间"的效果）。 */
export const PLAN_CENTER = {
  x: VIEW_BOX.x + VIEW_BOX.w / 2,
  y: VIEW_BOX.y + VIEW_BOX.h / 2,
}

/** 放大倍率：悬停"轻微抬起"，选中"真正放大"。
 *  ⚠️ 想让它更大/更小，改这一个数就行（房间被移到画布中心，放大不会溢出）。 */
export const SCALE_HOVER = 1.05
export const SCALE_SELECTED = 1.22

/**
 * 全部区域。`code` 必须与数据库 `rooms.code` 一致（它进 MQTT topic）。
 * `isCirculation: true` 表示这是交通空间，不是房间。
 */
export const AREAS = [
  { code: 'balcony', name: '阳台', x: 20, y: 20, w: 118, h: 170 },
  { code: 'living_room', name: '客厅', x: 138, y: 20, w: 214, h: 170 },
  { code: 'entryway', name: '玄关', x: 352, y: 95, w: 105, h: 95 },
  { code: 'kitchen', name: '厨房', x: 457, y: 20, w: 161, h: 170 },
  { code: 'hallway', name: '通道', x: 100, y: 190, w: 464, h: 42, isCirculation: true },
  { code: 'master_bedroom', name: '主卧', x: 100, y: 232, w: 168, h: 153 },
  { code: 'bathroom', name: '卫生间', x: 268, y: 232, w: 106, h: 153 },
  { code: 'second_bedroom', name: '次卧', x: 374, y: 232, w: 190, h: 153 },
]

/** 房间 code → 是否交通空间 */
export const CIRCULATION = new Set(AREAS.filter((a) => a.isCirculation).map((a) => a.code))

/**
 * 门 / 开口。
 *
 * 约定（为的是能用一句话描述、不用记四个方向）：
 *  - `'h'`：开口在**水平墙**上，墙的 y = `y`，开口从 `x` 到 `x + w`；**铰链在左端，向下方（+y）开**
 *  - `'v'`：开口在**垂直墙**上，墙的 x = `x`，开口从 `y` 到 `y + w`；**铰链在上端，向右方（+x）开**
 *
 * ⚠️ 开口的位置必须**正好落在某两个相邻区域的公共边上**，否则打不出来
 *    （`wallSegments()` 只会在完全包含该开口的边上挖洞）。
 *
 * 这组数据**只在 `WALL_OPENINGS = true` 时才生效**。
 */
export const DOORS = [
  // 入户门：从外侧进玄关（玄关上方那段是入户凹口，属于户外）
  { kind: 'h', x: 390, y: 95, w: 45 },
  // 客厅 ↔ 玄关
  { kind: 'v', x: 352, y: 125, w: 40 },
  // 客厅 → 通道
  { kind: 'h', x: 210, y: 190, w: 40 },
  // 玄关 → 通道
  { kind: 'h', x: 390, y: 190, w: 40 },
  // 厨房 → 通道
  { kind: 'h', x: 485, y: 190, w: 40 },
  // 通道 → 主卧 / 卫生间 / 次卧
  { kind: 'h', x: 160, y: 232, w: 40 },
  { kind: 'h', x: 295, y: 232, w: 40 },
  { kind: 'h', x: 430, y: 232, w: 40 },
]

/**
 * 墙上要不要留开口。
 *
 * ⚠️ 2026-10-16 用户要求「开口也不要了」→ **改为 false，墙现在是连续实墙**。
 *    想恢复开口（并配合 `FloorPlan.vue` 的 `SHOW_DOOR_SYMBOLS` 画门符号），
 *    把这里改成 `true` 即可，两部分互不影响：
 *      · `WALL_OPENINGS`  → 墙线是否挖洞
 *      · `SHOW_DOOR_SYMBOLS` → 是否画门扇/开启弧
 */
export const WALL_OPENINGS = false

/** 房间中心的用户坐标（放文字用） */
export function centerOf(area) {
  return { cx: area.x + area.w / 2, cy: area.y + area.h / 2 }
}

// ---------------------------------------------------------------------------
// 墙线生成：把每个区域的四条边拆成线段，**并挖掉被门覆盖的那一段**。
// ---------------------------------------------------------------------------

const EPS = 0.01

/** 从 [a, b] 区间里挖掉 openings 覆盖的部分，返回剩下的子区间。 */
function subtract(a, b, openings) {
  let parts = [[a, b]]
  for (const [oa, ob] of openings) {
    const next = []
    for (const [sa, sb] of parts) {
      if (ob <= sa + EPS || oa >= sb - EPS) {
        next.push([sa, sb]) // 不相交，原样保留
        continue
      }
      if (oa > sa + EPS) next.push([sa, oa]) // 左残段
      if (ob < sb - EPS) next.push([ob, sb]) // 右残段
    }
    parts = next
  }
  return parts.filter(([sa, sb]) => sb - sa > EPS)
}

/**
 * 生成一个区域自己的四条墙线（已按门打洞）。
 * 相邻区域会各自画一遍公共边 —— 因为两边的门开口是同一处，两条都会挖掉，不会漏。
 */
export function wallSegments(area) {
  const x1 = area.x
  const y1 = area.y
  const x2 = area.x + area.w
  const y2 = area.y + area.h

  // 用户要求"开口也不要了" → WALL_OPENINGS=false 时直接用空表，墙就是连续实墙
  const doors = WALL_OPENINGS ? DOORS : []

  const hOpen = (y) =>
    doors.filter((d) => d.kind === 'h' && Math.abs(d.y - y) < EPS).map((d) => [d.x, d.x + d.w])
  const vOpen = (x) =>
    doors.filter((d) => d.kind === 'v' && Math.abs(d.x - x) < EPS).map((d) => [d.y, d.y + d.w])

  const segs = []
  for (const [a, b] of subtract(x1, x2, hOpen(y1))) segs.push({ x1: a, y1, x2: b, y2: y1 }) // 上
  for (const [a, b] of subtract(x1, x2, hOpen(y2))) segs.push({ x1: a, y1: y2, x2: b, y2 }) // 下
  for (const [a, b] of subtract(y1, y2, vOpen(x1))) segs.push({ x1, y1: a, x2: x1, y2: b }) // 左
  for (const [a, b] of subtract(y1, y2, vOpen(x2))) segs.push({ x1: x2, y1: a, x2, y2: b }) // 右
  return segs
}

/** 门的开启弧线（四分之一圆）+ 门扇。用 SVG path 表达。 */
export function doorPaths(door) {
  const { kind, x, y, w } = door
  if (kind === 'h') {
    // 铰链 (x, y)，门扇垂到 (x, y+w)，弧从 (x+w, y) 扫到 (x, y+w)
    return {
      leaf: `M ${x} ${y} L ${x} ${y + w}`,
      arc: `M ${x + w} ${y} A ${w} ${w} 0 0 1 ${x} ${y + w}`,
    }
  }
  // 铰链 (x, y)，门扇伸到 (x+w, y)，弧从 (x, y+w) 扫到 (x+w, y)
  return {
    leaf: `M ${x} ${y} L ${x + w} ${y}`,
    arc: `M ${x} ${y + w} A ${w} ${w} 0 0 1 ${x + w} ${y}`,
  }
}
