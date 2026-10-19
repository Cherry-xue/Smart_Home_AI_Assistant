<template>
  <div ref="wrapEl" class="plan-wrap">
    <svg
      class="plan"
      :viewBox="planViewBox"
      preserveAspectRatio="xMidYMid meet"
      role="group"
      aria-label="户型图：点击房间查看详细数据与器件开关"
      @click="emit('select', null)"
    >
      <defs>
        <!-- 把"选中房间所占据的区域"从其他房间里挖掉。
             ⚠️ 为什么用遮罩而不是"把选中房间搬到 DOM 末尾"：
             搬动 DOM 会让 Vue 用 insertBefore 移动节点，**正在跑的 CSS 过渡会被打断**，
             表现就是"只有本来就在末尾的次卧有完整动画，其他房间没有"。
             遮罩方案下 DOM 顺序恒定不动，动画就正常了。

             ⚠️⚠️ 挖洞矩形**必须和选中房间用同一套过渡**（同一个 CSS 变量、同一个时长）。
             否则遮罩是瞬间生效的，而房间还要滑 260ms —— 你会先看到目标位置被挖出一个
             纯白色的房间大小的洞，房间才慢慢滑过去。这就是"点一下屏幕中心立刻出现
             一个纯白框"的成因。让两者同步，洞就和房间始终重合，看不出来。 -->
        <mask
          id="dim-cut"
          maskUnits="userSpaceOnUse"
          :x="viewBox.x - 300"
          :y="viewBox.y - 300"
          :width="viewBox.w + 600"
          :height="viewBox.h + 600"
        >
          <rect
            :x="viewBox.x - 300"
            :y="viewBox.y - 300"
            :width="viewBox.w + 600"
            :height="viewBox.h + 600"
            fill="#fff"
          />
          <rect
            class="mask-hole"
            :x="holeArea.x"
            :y="holeArea.y"
            :width="holeArea.w"
            :height="holeArea.h"
            fill="#000"
            :style="holeStyle"
          />
        </mask>
      </defs>

      <!-- 总览页**不显示底图**：底图只是编辑时的辅助参照（在「编辑房子」里看），
           总览要的是"房间 + 实时数据"，叠一张户型图反而看不清读数。
           所以这里既没有 <image>，也不再有"有底图/无底图"两套渲染 —— 统一一套。 -->

      <!-- 区域。⚠️ 这里**永远按 areas 的固定顺序**遍历，绝不重排 -->
      <g
        v-for="a in areas"
        :key="a.code"
        class="area"
        :class="areaClass(a)"
        :style="areaStyle(a)"
        :mask="isDimmed(a) ? 'url(#dim-cut)' : null"
        @click.stop="onAreaClick(a)"
        @mouseenter="onAreaEnter(a)"
        @mouseleave="emit('hover', null)"
      >
        <rect class="fill" :x="a.x" :y="a.y" :width="a.w" :height="a.h" />

        <line
          v-for="(s, i) in segsOf(a)"
          :key="`w${i}`"
          class="wall"
          :x1="s.x1"
          :y1="s.y1"
          :x2="s.x2"
          :y2="s.y2"
        />

        <!-- 房间名居中、读数在下面一行。
             行距用 `upx(18)`（屏幕像素换算成用户单位）而不是写死的 9/12 用户单位 ——
             画布很大时 1 用户单位只有零点几个像素，写死会让两行字叠在一起。 -->
        <text
          class="area-name"
          :x="a.x + a.w / 2"
          :y="a.y + a.h / 2 - (a.isCirculation ? 0 : upx(8))"
          text-anchor="middle"
          dominant-baseline="central"
          :style="{ fontSize: px(a.isCirculation ? 11 : 13) }"
        >
          {{ a.name }}
        </text>

        <text
          v-if="!a.isCirculation && showData(a)"
          class="area-data"
          :x="a.x + a.w / 2"
          :y="a.y + a.h / 2 + upx(13)"
          text-anchor="middle"
          dominant-baseline="central"
          :style="{ fontSize: px(11) }"
        >
          {{ dataLineFor(a) }}
        </text>
      </g>

      <!-- 门符号（门扇 + 开启弧）。默认关掉，墙上开口仍保留（那是打洞的结果）。 -->
      <g v-if="SHOW_DOOR_SYMBOLS" class="doors" :class="{ 'doors--hidden': !!selectedCode }">
        <path v-for="(d, i) in doorPathsList" :key="`l${i}`" class="door-leaf" :d="d.leaf" />
        <path v-for="(d, i) in doorPathsList" :key="`a${i}`" class="door-arc" :d="d.arc" />
      </g>
    </svg>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { pickKeyMetrics, keyMetricLine } from '@/utils/format'
import {
  AREAS as DEFAULT_AREAS,
  DOORS,
  VIEW_BOX as DEFAULT_VIEW_BOX,
  SCALE_HOVER,
  SCALE_SELECTED,
  centerOf,
  doorPaths,
  wallSegments,
} from './roomGeometry'

const props = defineProps({
  /** code → 房间卡片（/api/homes/1/rooms 的 items）；通道不在里面 */
  roomsByCode: { type: Object, default: () => ({}) },
  selectedCode: { type: String, default: null },
  hoverCode: { type: String, default: null },
  /**
   * 户型几何（来自 `GET /api/floorplan`）。
   *
   * 用到的字段：`view_box` / `areas`（`background` 只在编辑器里用，总览不画）。
   * **传 null 时回落到 `roomGeometry.js` 里写死的默认几何** —— 这样即使接口挂了，
   * 总览页也不是一片空白（缺省值本来就是"内置户型"）。
   */
  geometry: { type: Object, default: null },
})

const emit = defineEmits(['select', 'hover'])

// ---- 几何：接口优先，缺省回落内置 ----------------------------------------
const viewBox = computed(() => {
  const vb = props.geometry?.view_box
  if (!vb || !(vb.w > 0) || !(vb.h > 0)) return { ...DEFAULT_VIEW_BOX }
  return { x: vb.x ?? 0, y: vb.y ?? 0, w: vb.w, h: vb.h }
})
const planViewBox = computed(
  () => `${viewBox.value.x} ${viewBox.value.y} ${viewBox.value.w} ${viewBox.value.h}`,
)
const planCenter = computed(() => ({
  x: viewBox.value.x + viewBox.value.w / 2,
  y: viewBox.value.y + viewBox.value.h / 2,
}))

/** 把接口结构（`is_circulation`）转成组件内部一直用的驼峰（`isCirculation`）。
 *
 * ⚠️ 两种情况都不能回落内置几何：
 *    ① `empty`：别的套房还没画户型 —— 内置户型是 1 号房的，画上去就是"别人家"；
 *    ② `custom` 但框了 0 个房间：在用户的底图上叠一张对不上的默认户型。
 *    此时正确地"什么都不画"，由页面提示去编辑。
 */
const areas = computed(() => {
  const g = props.geometry
  const list = g?.areas
  const own = g?.source === 'custom' || g?.source === 'empty'
  if (own && Array.isArray(list) && list.length === 0) return []
  if (!Array.isArray(list) || !list.length) return DEFAULT_AREAS
  return list.map((a) => ({
    code: a.code,
    name: a.name || a.code,
    x: Number(a.x) || 0,
    y: Number(a.y) || 0,
    w: Number(a.w) || 0,
    h: Number(a.h) || 0,
    isCirculation: !!a.is_circulation,
  }))
})



/** 有底图时墙已经画在图里了，不再叠一层线（否则两条墙互相错位，很脏） */

const SEGMENTS = computed(() =>
  Object.fromEntries(areas.value.map((a) => [a.code, wallSegments(a)])),
)
const doorPathsList = computed(() => DOORS.map(doorPaths))

/** 是否画门符号（门扇 + 开启弧）。默认关闭。 */
const SHOW_DOOR_SYMBOLS = false

// ---- 字号不随容器缩放 ------------------------------------------------------
// ⚠️ SVG 是整体缩放的：左右面板一展开，画布宽度可能掉一半，
//    字号若写死成 viewBox 单位，字会跟着缩到看不见。
//    这里量出真实缩放比，字号除以它 → 屏幕上的字号恒定。
const wrapEl = ref(null)
const planScale = ref(1)
let ro = null

function px(screenPx) {
  const s = planScale.value || 1
  return `${(screenPx / s).toFixed(2)}px`
}

/** 屏幕像素 → 用户单位。字号与行距都走它，保证任何画布尺寸下屏幕观感一致。 */
function upx(screenPx) {
  return screenPx / (planScale.value || 1)
}

/**
 * 房间太矮时只画名字、不画读数。
 *
 * 两行字总高约 34 屏幕像素；房间不够高时硬塞会溢出到隔壁房间，
 * 看起来像文字错位（"阳台"和"无数据"叠在一起就是这个原因）。
 */
function showData(a) {
  return a.h >= upx(34)
}

onMounted(() => {
  if (!wrapEl.value) return
  ro = new ResizeObserver(([entry]) => {
    const { width, height } = entry.contentRect
    if (!width || !height) return
    planScale.value = Math.min(width / viewBox.value.w, height / viewBox.value.h) || 1
  })
  ro.observe(wrapEl.value)
})
onBeforeUnmount(() => ro?.disconnect())
watch(viewBox, () => {
  // 换户型（画布尺寸变了）之后重新量一次缩放比，否则字号会突然变大/变小
  if (!wrapEl.value) return
  const { width, height } = wrapEl.value.getBoundingClientRect()
  if (width && height) {
    planScale.value = Math.min(width / viewBox.value.w, height / viewBox.value.h) || 1
  }
})

// ---- 选中态 ----------------------------------------------------------------
const selectedArea = computed(() =>
  props.selectedCode ? areas.value.find((a) => a.code === props.selectedCode) ?? null : null,
)

const isDimmed = (a) => !!props.selectedCode && props.selectedCode !== a.code

/**
 * 遮罩里那块"挖洞"矩形**所在的区域**。
 *
 * ⚠️ 矩形必须**始终存在**（不能 `v-if`）：新建的节点不会触发 CSS 过渡，
 *    洞就会瞬间跳到目标位置。所以这里记的是"最近一次选中过的区域"，
 *    取消选中时矩形还在，只是变换回到原位。
 */
const holeArea = ref(areas.value[0] ?? DEFAULT_AREAS[0])
watch(
  () => props.selectedCode,
  (code) => {
    if (!code) return
    const a = areas.value.find((x) => x.code === code)
    if (a) holeArea.value = a
  },
)
// 换了户型（比如刚保存完新框选），旧坐标的洞必须换成新坐标，否则会挖错地方
watch(areas, (list) => {
  const code = props.selectedCode
  const a = code ? list.find((x) => x.code === code) : null
  holeArea.value = a ?? list[0] ?? DEFAULT_AREAS[0]
})

/**
 * 挖洞矩形的变换 —— **与 `.area--selected` 用同一套 CSS 变量、同一段过渡**。
 *
 * 这样"洞"和"房间"同步移动、始终重合，就不会出现"点一下先在中心露出一个白框、
 * 房间才慢慢滑过去"。未选中时返回空对象 → 变量回落默认值 → 动画回原位。
 */
const holeStyle = computed(() => {
  const a = selectedArea.value
  if (!a) return {}
  const { cx, cy } = centerOf(a)
  return {
    '--sc': SCALE_SELECTED,
    '--tx': `${planCenter.value.x - cx}px`,
    '--ty': `${planCenter.value.y - cy}px`,
  }
})

// ---- 交互 ------------------------------------------------------------------
function onAreaClick(a) {
  // 通道不是房间，点它等同于点空白 → 退出
  if (a.isCirculation) {
    emit('select', null)
    return
  }
  // ⚠️ 关键交互约定：**已经选中某个房间时，点任何别的区域都是"退出"**，
  //    而不是"换成那个房间"。想换房间就先点空白退出，再点新房间。
  if (props.selectedCode && props.selectedCode !== a.code) {
    emit('select', null)
    return
  }
  emit('select', a.code)
}

function onAreaEnter(a) {
  if (a.isCirculation) return
  emit('hover', a.code)
}

function areaClass(a) {
  const out = []
  if (a.isCirculation) out.push('area--circulation')
  // ⚠️ 通道也必须跟着一起模糊 —— 它只是"不可点选"，但仍属于"其他区域"。
  if (props.selectedCode) {
    out.push(props.selectedCode === a.code ? 'area--selected' : 'area--dim')
  } else if (!a.isCirculation && props.hoverCode === a.code) {
    out.push('area--hover')
  }
  return out
}

function areaStyle(a) {
  if (a.isCirculation) return {}
  if (props.selectedCode === a.code) {
    // 放大 **+ 平移到画布中心**（平移量必须在 viewBox 用户坐标里算）
    const { cx, cy } = centerOf(a)
    return {
      '--sc': SCALE_SELECTED,
      '--tx': `${planCenter.value.x - cx}px`,
      '--ty': `${planCenter.value.y - cy}px`,
    }
  }
  if (props.selectedCode) return {}
  if (props.hoverCode === a.code) return { '--sc': SCALE_HOVER }
  return {}
}

const segsOf = (a) => SEGMENTS.value[a.code] ?? []

// ---- 房间里那行小字 --------------------------------------------------------
// ⚠️ 必须按房间宽度截断：玄关/卫生间很窄，两三个指标一拼就会**溢出房间边界**。
function textUnits(s) {
  let u = 0
  for (const ch of s) u += /[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? 1 : 0.56
  return u
}

function fitText(s, availUnits) {
  if (textUnits(s) <= availUnits) return s
  let out = ''
  let u = 0
  for (const ch of s) {
    const cu = /[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? 1 : 0.56
    if (u + cu > availUnits - 0.9) break
    out += ch
    u += cu
  }
  return `${out}…`
}

/** 窄房间只放 1 个指标，宽房间放 2 个。 */
function dataLineFor(a) {
  const room = props.roomsByCode[a.code]
  if (!room) return '—'
  const avail = (a.w - 16) / 11 // 11 是这一行的屏幕字号
  const want = avail >= 11 ? 2 : 1
  const picked = pickKeyMetrics(room.key_metrics, want)
  if (!picked.length) return '无数据'
  const full = picked.map(keyMetricLine).join(' · ')
  if (textUnits(full) <= avail) return full
  // 拼两个太宽就退回一个
  const one = keyMetricLine(picked[0])
  return fitText(textUnits(one) <= avail ? one : full, avail)
}
</script>

<style scoped>
.plan-wrap {
  width: 100%;
  height: 100%;
  min-height: 0;
}

.plan {
  width: 100%;
  height: 100%;
  display: block;
  /* ⚠️ 必须 visible：放大后的房间会超出 viewBox，默认会被裁掉一角 */
  overflow: visible;
}

.area {
  cursor: pointer;
  /* 以"自身包围盒中心"为缩放原点 —— 不用手算 translate 的原点，CSS 就能补间。
     选中的房间会带上 --tx/--ty，因此表现为"放大并滑到中间"。 */
  transform-box: fill-box;
  transform-origin: center;
  transform: translate(var(--tx, 0px), var(--ty, 0px)) scale(var(--sc, 1));
  transition:
    transform var(--dur) var(--ease),
    opacity var(--dur) var(--ease),
    filter var(--dur) var(--ease);
}

.area--circulation {
  cursor: default;
}

.area .fill {
  fill: var(--c-room);
  transition:
    fill var(--dur) var(--ease),
    fill-opacity var(--dur) var(--ease);
}
.area--circulation .fill {
  fill: var(--c-room-circulation);
}

/* ---- 有底图时：填充变成半透明色块，图能透出来 ---- */
/* 选中的房间要给一块**接近不透明**的底，否则照片上的文字读不清 */
.area:hover .fill {
  fill: var(--c-room-hover);
}
/* 通道不参与悬停高亮（它不可点选，跟着变亮会误导） */
.area--circulation:hover .fill {
  fill: var(--c-room-circulation);
}

.area--dim {
  opacity: 0.24;
  filter: blur(4px);
}
.area--dim:hover {
  opacity: 0.5;
}
/* 通道不能因为鼠标掠过就"浮"回来 */
.area--circulation.area--dim:hover {
  opacity: 0.24;
}

.area--selected .fill {
  fill: var(--c-surface);
}

.wall {
  stroke: var(--c-wall);
  stroke-width: 2;
  /* 墙不跟着放大变粗 */
  vector-effect: non-scaling-stroke;
}
.area--selected .wall {
  stroke: var(--c-accent);
  stroke-width: 2.4;
}

/* 遮罩里的"挖洞"矩形。
   ⚠️ 必须和 .area 用**同一套 transform 表达式与同一段过渡**，
   否则洞会瞬间跳到目标位置，露出一个白框。 */
.mask-hole {
  transform-box: fill-box;
  transform-origin: center;
  transform: translate(var(--tx, 0px), var(--ty, 0px)) scale(var(--sc, 1));
  transition: transform var(--dur) var(--ease);
}

.doors {
  transition: opacity var(--dur) var(--ease);
}
.doors--hidden {
  opacity: 0;
}
.door-arc,
.door-leaf {
  fill: none;
  stroke: var(--c-wall);
  stroke-width: 1;
  vector-effect: non-scaling-stroke;
  opacity: 0.55;
}

.area-name {
  fill: var(--c-ink);
  font-weight: 500;
  pointer-events: none;
}
.area-data {
  fill: var(--c-ink-muted);
  pointer-events: none;
}
.area--circulation .area-name {
  fill: var(--c-ink-muted);
  font-weight: 400;
}
.area--selected .area-name {
  fill: var(--c-accent);
}
.area--selected .area-data {
  fill: var(--c-ink-soft);
}

/* 底图模式下的角落标签：白色描边垫在文字下面，任何底色/照片上都读得清 */
/* 有底图时唯一的一行字：靠右下角对齐，避开底图自带的房间名（一般在中间/偏上） */
.area-data--corner {
  text-anchor: end;
  paint-order: stroke;
  stroke: var(--c-surface);
  stroke-width: 3.2px;
  stroke-linejoin: round;
  stroke-linecap: round;
}
</style>
