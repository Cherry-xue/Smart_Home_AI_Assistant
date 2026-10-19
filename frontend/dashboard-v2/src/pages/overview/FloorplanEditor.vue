<template>
  <el-dialog
    :model-value="modelValue"
    title="编辑房子"
    width="min(1040px, 96vw)"
    top="3vh"
    class="plan-dialog"
    :close-on-click-modal="false"
    @update:model-value="close"
  >
    <!-- 标题行直接放"套房名称"：房名和户型本来就是一体的，也省一行高度给画布 -->
    <template #header>
      <div class="ed-head">
        <span class="ed-head-title">编辑房子</span>
        <span class="ed-head-label">套房名称</span>
        <el-input
          v-model="formName"
          size="small"
          maxlength="64"
          show-word-limit
          placeholder="给这套房起个名字"
          class="ed-house-input"
        />
      </div>
    </template>
    <!-- ==================== 工具条 ==================== -->
    <div class="ed-toolbar">
      <el-upload
        :auto-upload="false"
        :show-file-list="false"
        accept="image/png,image/jpeg,image/webp"
        :on-change="onFileChange"
      >
        <el-button size="small" :loading="uploading">上传户型底图</el-button>
      </el-upload>

      <!-- 只删底图：框选、房间、画布尺寸都不动 -->
      <el-button v-if="background" size="small" :loading="removingBg" @click="removeBg">
        删除底图
      </el-button>

      <span class="hint">
        拖拽画房间，<b>松手打字命名</b>；点中框可拖动、拖角改大小
      </span>

      <div class="spacer" />

      <!-- 缩放只改"看哪一块"，不改画布尺寸 —— 保存时写的仍是 baseBox -->
      <el-button size="small" :disabled="!canZoomIn" @click="applyZoom(ZOOM_STEP)">放大</el-button>
      <el-button size="small" :disabled="!canZoomOut" @click="applyZoom(1 / ZOOM_STEP)">
        缩小
      </el-button>
      <el-button size="small" :disabled="!isZoomed" @click="resetZoom">还原</el-button>
      <span class="zoom-pct tnum" title="当前缩放比例">{{ Math.round(zoom * 100) }}%</span>

      <el-button size="small" :disabled="!areas.length" @click="clearFrames">清空所有框</el-button>
      <el-button size="small" @click="resetAll">恢复默认</el-button>
    </div>

    <!-- ==================== 画布 ==================== -->
    <div class="ed-stage">
      <svg
        ref="svgEl"
        class="ed-svg"
        :class="{ 'ed-svg--panning': !!panning }"
        :viewBox="`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`"
        preserveAspectRatio="xMidYMid meet"
        @pointerdown="onCanvasDown"
        @pointermove="onMove"
        @pointerup="onUp"
        @pointercancel="onUp"
        @wheel.prevent="onWheel"
        @contextmenu.prevent
        @dragstart.prevent
      >
        <!-- 兜底命中层：整个画布都能起手。
             ⚠️ 底图必须 `pointer-events:none`，否则浏览器会把拖拽当成"拖图片"，
                直接发 pointercancel —— 我们的兜底就拿到 0×0，误报"框太小"。
             ⚠️ 这里的尺寸用 `baseBox`（画布本体），不能用 `viewBox`（当前视口）：
                缩放后视口变小，若按视口铺，底图会被拉伸到放大后的那一小块上。 -->
        <rect
          :x="baseBox.x"
          :y="baseBox.y"
          :width="baseBox.w"
          :height="baseBox.h"
          class="ed-canvas"
        />
        <image
          v-if="background"
          :href="background.url"
          :x="baseBox.x"
          :y="baseBox.y"
          :width="baseBox.w"
          :height="baseBox.h"
          preserveAspectRatio="none"
          opacity="0.95"
          draggable="false"
          pointer-events="none"
        />

        <!-- 已画的框：按固定顺序渲染，选中时追加 8 个缩放手柄 -->
        <g v-for="(a, i) in areas" :key="`box-${i}`">
          <rect
            :x="a.x"
            :y="a.y"
            :width="a.w"
            :height="a.h"
            :class="[
              'ed-rect',
              a.code ? '' : 'ed-rect--unassigned',
              activeIndex === i ? 'ed-rect--active' : '',
            ]"
            @pointerdown="startMove($event, i)"
          />

          <!-- 房间名标签：贴在框的左上角。
               框里只画一个色块的话，20 个房间根本分不清谁是谁；
               标签带底色，压在任何底图上都读得清。 -->
          <g
            v-if="a.w > 18 && a.h > 14"
            class="ed-tag"
            :class="{ 'ed-tag--unassigned': !a.code }"
            pointer-events="none"
          >
            <rect
              :x="a.x + 3"
              :y="a.y + 3"
              :width="tagWidth(a)"
              :height="tagHeight"
              rx="3"
            />
            <text
              :x="a.x + 3 + tagPad"
              :y="a.y + 3 + tagHeight - tagFs * 0.35"
              :style="{ fontSize: `${tagFs}px` }"
            >
              {{ tagText(a) }}
            </text>
          </g>

          <!-- 手柄尺寸用用户单位：画布越大手柄相对越小，所以取 12 单位保底（900 宽时约 7px） -->
          <template v-if="activeIndex === i">
            <rect
              v-for="h in handlesOf(a)"
              :key="`h-${h.k}`"
              class="ed-handle"
              :class="`ed-handle--${h.k}`"
              :x="h.x"
              :y="h.y"
              :width="handleSize"
              :height="handleSize"
              @pointerdown="startResize($event, i, h.k)"
            />
          </template>
        </g>

        <!-- 正在拖的新框 -->
        <rect
          v-if="draft"
          class="ed-draft"
          :x="draft.x"
          :y="draft.y"
          :width="draft.w"
          :height="draft.h"
        />
      </svg>
    </div>

    <!-- ==================== 框列表 ==================== -->
    <div class="ed-list">
      <!-- 固定 4 行高度：房间再多也只在这里滚，不要把整个弹窗撑到出页面滚动条 -->
      <div class="ed-rows">
        <div v-if="!areas.length" class="ed-empty">
          还没有框选任何房间。{{ background ? '在上面的底图上拖拽画框。' : '先上传户型底图，或直接画。' }}
        </div>

        <div
          v-for="(a, i) in areas"
          :key="`row-${i}`"
          class="ed-row"
          :class="{ 'ed-row--active': activeIndex === i }"
          @click="activeIndex = i"
        >
        <span class="ed-swatch" :class="{ 'ed-swatch--circ': a.is_circulation }" />
        <span class="ed-pos tnum">
          {{ Math.round(a.x) }},{{ Math.round(a.y) }} · {{ Math.round(a.w) }}×{{ Math.round(a.h) }}
        </span>

        <!-- 这里选的是**房间类型**，不是某一间具体的房间。
             类型可以直接输入新的（allow-create），不做约束。 -->
        <el-select
          :model-value="a.type"
          placeholder="房间类型"
          size="small"
          filterable
          allow-create
          default-first-option
          class="ed-select"
          @update:model-value="(v) => assignType(a, v)"
        >
          <el-option v-for="t in TYPE_OPTIONS" :key="t" :label="t" :value="t" />
        </el-select>

        <!-- 房间名：用户自己起，**走廊以外不许重名**（重名时输入框变红并拦住保存） -->
        <el-input
          :ref="(el) => setRowRef(el, i)"
          v-model="a.name"
          size="small"
          maxlength="24"
          show-word-limit
          :placeholder="a.is_circulation ? '走廊名（可重名）' : '房间名（不可重名）'"
          :class="['ed-name', { 'ed-name--dup': !!nameConflict(a) }]"
          :title="nameConflict(a) ? `已有同名房间「${nameConflict(a)}」` : ''"
          @keyup.enter="onNameEnter(i)"
        />

          <el-button size="small" text type="danger" @click.stop="onRemove(i)">删除</el-button>
        </div>
      </div>

      <div v-if="areas.length" class="ed-summary">
        共 {{ areas.length }} 个框，已指定 {{ assignedCount }} 个
        <span v-if="unassignedCount" class="warn">· 还有 {{ unassignedCount }} 个没命名</span>
        <span v-if="conflicts.length" class="warn">
          · 房间名重复：{{ conflicts.join('、') }}（走廊可重名）
        </span>
        <span v-if="pendingDeletes.length" class="warn">
          · 保存后删除：{{ pendingDeletes.map((x) => x.name).join('、') }}
        </span>
      </div>

      <!--
        房间列表里有、但户型图上没画框的房间。
        这些房间没有任何一行可以点删除 —— 不给「移除」入口的话，用户只能被永久提示。
        来源通常是"新建套房时自动带的占位房间"，或者只在设备页建过、户型还没画。
      -->
      <div v-if="missingRooms.length" class="ed-missing">
        <span class="ed-missing-title">图上没有这些房间：</span>
        <span v-for="r in missingRooms" :key="r.code" class="ed-missing-chip">
          {{ r.name }}<template v-if="r.device_count">（{{ r.device_count }} 台设备）</template>
          <button class="ed-missing-del" title="从房间列表里删掉这个房间" @click.stop="removeMissingRoom(r)">
            移除
          </button>
        </span>
        <span class="ed-missing-hint">画个框给它，或点「移除」删掉这个房间</span>
      </div>
    </div>

    <template #footer>
      <span class="footer-hint">
        <template v-if="background">
          底图 {{ background.width }}×{{ background.height }}
        </template>
        <template v-else>内置默认户型坐标</template>
        · 滚轮缩放 · 右键拖动平移
      </span>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createRoom,
  deleteRoom,
  removeFloorplanImage,
  renameHome,
  resetFloorplan,
  saveFloorplan,
  uploadFloorplanImage,
} from '@/api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 当前套房 id —— 几何、底图、改名都按套房隔离 */
  homeId: { type: Number, default: 1 },
  /** 当前套房名（顶部那个名字，和房间名不是一回事） */
  homeName: { type: String, default: '' },
  /** GET /api/floorplan 的返回体 */
  geometry: { type: Object, default: null },
  /** GET /api/homes/{id}/rooms 的 items（用来给框指派房间） */
  rooms: { type: Array, default: () => [] },
})

const emit = defineEmits(['update:modelValue', 'saved', 'rooms-changed', 'renamed'])

const svgEl = ref(null)
const areas = ref([])
/**
 * 画布本体的范围 —— **保存时写的 view_box 就是它**，缩放永远不改它。
 *
 * 拆成两个变量的原因：缩放要改 `viewBox`（看哪一块），但保存必须用原始画布范围，
 * 否则"放大后保存"会把画布裁成放大后的那一小块，边框和底图全错位。
 */
const baseBox = ref({ x: 0, y: 0, w: 628, h: 395 })
/** 当前视口（缩放 / 平移后）。元素坐标始终是 baseBox 坐标系里的值。 */
const viewBox = ref({ x: 0, y: 0, w: 628, h: 395 })
const background = ref(null)
const activeIndex = ref(-1)
const saving = ref(false)
/** 本次编辑里"删掉了框"的真实房间：保存时统一删除（取消编辑则什么都不删） */
const pendingDeletes = ref([])
/** 编辑中的套房名。保存时才提交，取消不影响。 */
const formName = ref('')
const uploading = ref(false)
const removingBg = ref(false)

const MIN_SIZE = 12
const HANDLE = 12

// ---- 缩放 / 平移 -----------------------------------------------------------
const ZOOM_MIN = 0.4 // 缩小到 40%（看全貌）
const ZOOM_MAX = 6 // 放大 6 倍（画细节）
const ZOOM_STEP = 1.25

/** 当前倍率：视口越小 = 放得越大 */
const zoom = computed(() => baseBox.value.w / viewBox.value.w)
const isZoomed = computed(() => Math.abs(zoom.value - 1) > 1e-6)
const canZoomIn = computed(() => zoom.value < ZOOM_MAX - 1e-6)
const canZoomOut = computed(() => zoom.value > ZOOM_MIN + 1e-6)
/** 手柄尺寸反向缩放：屏幕上的视觉大小保持恒定，否则放大后手柄会大得挡住框 */
const handleSize = computed(() => HANDLE / Math.max(0.2, zoom.value))

/** 视口夹紧：贴在画布内；缩得比画布还小时居中留白 */
function clampView(box) {
  const b = baseBox.value
  let { x, y, w, h } = box
  if (w >= b.w) x = b.x + (b.w - w) / 2
  else x = Math.min(Math.max(x, b.x), b.x + b.w - w)
  if (h >= b.h) y = b.y + (b.h - h) / 2
  else y = Math.min(Math.max(y, b.y), b.y + b.h - h)
  return { x, y, w, h }
}

/** 以某个用户坐标点为锚缩放（点住的地方内容不动） */
function zoomAt(factor, anchor) {
  const b = baseBox.value
  const cur = viewBox.value
  const curZoom = b.w / cur.w
  const next = Math.min(Math.max(curZoom * factor, ZOOM_MIN), ZOOM_MAX)
  if (Math.abs(next - curZoom) < 1e-9) return

  const w = b.w / next
  const h = b.h / next
  // 锚点在当前视口里的相对位置，缩放后保持不变
  const rx = (anchor.x - cur.x) / cur.w
  const ry = (anchor.y - cur.y) / cur.h
  viewBox.value = clampView({ x: anchor.x - rx * w, y: anchor.y - ry * h, w, h })
}

/** 按钮缩放：以视口中心为锚 */
function applyZoom(factor) {
  const cur = viewBox.value
  zoomAt(factor, { x: cur.x + cur.w / 2, y: cur.y + cur.h / 2 })
}

function resetZoom() {
  viewBox.value = { ...baseBox.value }
}

function onWheel(e) {
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12
  zoomAt(factor, toUser(e))
}

/** 中键拖动平移。放大后没有它就只能看中间那一块 */
const panning = ref(null)

// 统一入口：打开 / 上传 / 重置都用它，避免 baseBox 与 viewBox 各改一边
function setBox(box) {
  const w = Number(box?.w)
  const h = Number(box?.h)
  const b = {
    x: Number(box?.x) || 0,
    y: Number(box?.y) || 0,
    w: w > 0 ? w : 628,
    h: h > 0 ? h : 395,
  }
  baseBox.value = { ...b }
  viewBox.value = { ...b }
}

// ---- 房间名标签：尺寸跟着 viewBox 走 --------------------------------------
/** 字号按画布宽度换算：底图 2000 宽时用 9 号字，在屏幕上会小到看不见。 */
const tagFs = computed(() => Math.min(26, Math.max(12, viewBox.value.w * 0.02)))
const tagHeight = computed(() => tagFs.value * 1.55)
const tagPad = computed(() => tagFs.value * 0.5)

function tagText(a) {
  // 只在框里打了字、还没保存的，也要立刻显示出来 —— 否则打完字看不到反馈，
  // 用户会以为没生效
  return (a.name || '').trim() || (a.code ? a.code : '未指定房间')
}

/** 估宽：中文按 1 个字号、ASCII 按 0.55（够用，不做真实测量）。 */
function tagWidth(a) {
  let units = 0
  for (const ch of tagText(a)) units += /[\x00-\xff]/.test(ch) ? 0.55 : 1
  return units * tagFs.value + tagPad.value * 2
}

// ---- 坐标换算 --------------------------------------------------------------
/** 屏幕坐标 → SVG 用户坐标（getScreenCTM 反变换，容器怎么缩放都对）。 */
function toUser(evt) {
  const svg = svgEl.value
  if (!svg) return { x: 0, y: 0 }
  const pt = svg.createSVGPoint()
  pt.x = evt.clientX
  pt.y = evt.clientY
  const m = svg.getScreenCTM()
  if (!m) return { x: 0, y: 0 }
  const p = pt.matrixTransform(m.inverse())
  return { x: p.x, y: p.y }
}

function capture(evt) {
  // 拖出画布外还要继续收到 move/up。合成事件（自动化测试）里 pointerId 不是活跃指针，
  // 捕获会抛 NotFoundError，所以必须 try/catch —— 否则自动化一点就崩。
  try {
    svgEl.value?.setPointerCapture?.(evt.pointerId)
  } catch {
    /* 忽略 */
  }
}

// ---- 新建框（在空白处拖）---------------------------------------------------
const drawing = ref(false)
const drawFrom = ref(null)
const draft = ref(null)

function onCanvasDown(e) {
  // 右键 = 平移画布（放大后靠它挪视野）。左键才是画框。
  if (e.button === 2) {
    e.preventDefault()
    const m = svgEl.value?.getScreenCTM()
    panning.value = {
      sx: e.clientX,
      sy: e.clientY,
      box: { ...viewBox.value },
      // 屏幕像素 → 用户单位的系数（视口宽度不变，整个拖动过程中稳定）
      scale: m?.a || 1,
    }
    capture(e)
    return
  }
  if (e.button !== undefined && e.button !== 0) return
  // 阻止浏览器默认行为（拖图片 / 划选文字）：否则拖到一半会收到 pointercancel，
  // 我们只能拿到 0×0，用户就看到"框太小"
  e.preventDefault()
  const p = toUser(e)
  // 缩小视图时画布四周是空白，点那里不该开始画框（否则会得到一个贴边的怪框）
  const b = baseBox.value
  if (p.x < b.x || p.x > b.x + b.w || p.y < b.y || p.y > b.y + b.h) return
  drawing.value = true
  drawFrom.value = p
  draft.value = { x: p.x, y: p.y, w: 0, h: 0 }
  activeIndex.value = -1
  capture(e)
}

// ---- 拖动 / 缩放已有框 -----------------------------------------------------
/** {mode:'move'|'resize', handle, from:{x,y}, orig:{x,y,w,h}, index} */
const drag = ref(null)

function startMove(e, i) {
  // 右键留给画布平移：直接返回、且不要 stopPropagation，让事件冒泡到 svg
  if (e.button === 2) return
  if (e.button !== undefined && e.button !== 0) return
  activeIndex.value = i
  const a = areas.value[i]
  drag.value = {
    mode: 'move',
    from: toUser(e),
    orig: { x: a.x, y: a.y, w: a.w, h: a.h },
    index: i,
  }
  capture(e)
  e.stopPropagation()
}

function startResize(e, i, handle) {
  // 同上：右键落在手柄上也要能平移画布
  if (e.button === 2) return
  if (e.button !== undefined && e.button !== 0) return
  activeIndex.value = i
  const a = areas.value[i]
  drag.value = {
    mode: 'resize',
    handle,
    from: toUser(e),
    orig: { x: a.x, y: a.y, w: a.w, h: a.h },
    index: i,
  }
  capture(e)
  e.stopPropagation()
}

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi)

function onMove(e) {
  // 0) 中键平移：只挪视口，不动任何元素坐标
  if (panning.value) {
    const { sx, sy, box, scale } = panning.value
    // 鼠标往右拖 = 画面往右走 = 视口往左移
    const x = box.x - (e.clientX - sx) / scale
    const y = box.y - (e.clientY - sy) / scale
    viewBox.value = clampView({ x, y, w: box.w, h: box.h })
    return
  }

  // 1) 拖动 / 缩放已有框
  if (drag.value) {
    const { from, orig, index } = drag.value
    const a = areas.value[index]
    if (!a) return
    const p = toUser(e)
    const dx = p.x - from.x
    const dy = p.y - from.y
    // ⚠️ 边界用 baseBox（画布），不是 viewBox（当前视口）：
    //    用视口的话放大后框会被夹在那一小块里、缩小后又能拖到画布外。
    const vb = baseBox.value

    if (drag.value.mode === 'move') {
      a.x = Math.round(clamp(orig.x + dx, vb.x, vb.x + vb.w - orig.w))
      a.y = Math.round(clamp(orig.y + dy, vb.y, vb.y + vb.h - orig.h))
      return
    }

    // 缩放：按手柄方向调整；不允许小于 MIN_SIZE，也不允许出画布
    const h = drag.value.handle
    let { x, y, w, h: hh } = orig

    if (h.includes('w')) {
      const nx = clamp(x + dx, vb.x, x + w - MIN_SIZE)
      w += x - nx
      x = nx
    }
    if (h.includes('e')) {
      w = clamp(w + dx, MIN_SIZE, vb.x + vb.w - x)
    }
    if (h.includes('n')) {
      const ny = clamp(y + dy, vb.y, y + hh - MIN_SIZE)
      hh += y - ny
      y = ny
    }
    if (h.includes('s')) {
      hh = clamp(hh + dy, MIN_SIZE, vb.y + vb.h - y)
    }

    a.x = Math.round(x)
    a.y = Math.round(y)
    a.w = Math.round(w)
    a.h = Math.round(hh)
    return
  }

  // 2) 画新框
  if (!drawing.value || !drawFrom.value) return
  const p = toUser(e)
  draft.value = {
    x: Math.min(p.x, drawFrom.value.x),
    y: Math.min(p.y, drawFrom.value.y),
    w: Math.abs(p.x - drawFrom.value.x),
    h: Math.abs(p.y - drawFrom.value.y),
  }
}

function onUp(e) {
  if (panning.value) {
    panning.value = null
    return
  }
  if (drag.value) {
    drag.value = null
    return
  }
  if (!drawing.value) return
  drawing.value = false

  // 系统把指针收走了（原生拖拽 / 手势接管）：这不是"画得太小"，静默中止
  if (e?.type === 'pointercancel') {
    draft.value = null
    drawFrom.value = null
    return
  }

  // ⚠️ 用「抬手这一下」的坐标补最后一次移动。
  //    快速拖动时浏览器可能一个 pointermove 都没派发（事件被合并/频率限制），
  //    只靠 draft 会拿到 0×0 —— 表现就是"不管画多大都提示框太小"。
  if (e?.type === 'pointerup' && drawFrom.value) {
    const p = toUser(e)
    draft.value = {
      x: Math.min(p.x, drawFrom.value.x),
      y: Math.min(p.y, drawFrom.value.y),
      w: Math.abs(p.x - drawFrom.value.x),
      h: Math.abs(p.y - drawFrom.value.y),
    }
  }

  const d = draft.value
  draft.value = null
  drawFrom.value = null
  if (!d) return
  // 只是点了一下（几乎没位移）→ 当误触，什么都不做。
  // 以前这里会报"框太小"，用户点一下画布就被吓一次。
  if (d.w < 2 && d.h < 2) return

  // 夹到画布内：缩小视图时画布外是空白，那里画出来的框没有意义
  const b = baseBox.value
  const nx = Math.max(d.x, b.x)
  const ny = Math.max(d.y, b.y)
  const nw = Math.min(d.x + d.w, b.x + b.w) - nx
  const nh = Math.min(d.y + d.h, b.y + b.h) - ny

  if (nw < MIN_SIZE || nh < MIN_SIZE) {
    ElMessage({ message: `框太小（至少 ${MIN_SIZE}×${MIN_SIZE}），再画一次`, type: 'warning' })
    return
  }
  areas.value.push({
    code: '',
    name: '',
    x: Math.round(nx),
    y: Math.round(ny),
    w: Math.round(nw),
    h: Math.round(nh),
    is_circulation: false,
  })
  activeIndex.value = areas.value.length - 1
  // 松手就把光标放进名字输入框：用户不用再去点一下，直接打字就是房间名
  focusNameRow(areas.value.length - 1)
}

// ---- 行内命名 --------------------------------------------------------------
const nameInputs = ref([])

function setRowRef(el, i) {
  nameInputs.value[i] = el
}

function focusNameRow(i) {
  nextTick(() => {
    const el = nameInputs.value[i]
    el?.focus?.()
    // 顺便把已有的字选中：改名字时直接覆盖，不用先删
    el?.select?.()
  })
}

/** 回车 = 认下这个名字（真正的新建发生在保存时，取消编辑不会留下空房间）。 */
function onNameEnter(i) {
  const a = areas.value[i]
  if (!a) return
  a.name = (a.name || '').trim()
  if (!a.type) a.type = guessType(a.name, a.is_circulation)

  const dup = nameConflict(a)
  if (dup) {
    ElMessage({
      message: `已有同名房间「${dup}」，换个名字（走廊可以同名）`,
      type: 'warning',
      duration: 3000,
    })
    return
  }
  ElMessage({ message: `「${a.name || '未命名'}」已记录，保存后生效`, type: 'success', duration: 2000 })
}

/**
 * 保存前把"只写了名字"的框落成真实房间。
 *
 * 放在保存时而不是打字时创建，是为了让「取消」真的什么都不留下 ——
 * 否则用户拖了 10 个框、打了名字又取消，库里会多出 10 个空房间。
 */
async function resolveRooms() {
  // 名字 → 该名字下所有房间编码（可能有多间同名房间）
  const byName = new Map()
  for (const r of props.rooms || []) {
    if (!byName.has(r.name)) byName.set(r.name, [])
    byName.get(r.name).push(r.code)
  }
  // 已经被别的框占用的房间不能再画第二次
  const used = new Set(areas.value.filter((a) => a.code).map((a) => a.code))
  let created = 0

  for (const a of areas.value) {
    if (a.code) continue // 交通空间在选的时候就带了 hallway_N
    const nm = (a.name || '').trim()

    // 有一间同名的、且没被占用的 → 复用；否则新建一间同名的
    const free = (byName.get(nm) || []).find((c) => !used.has(c))
    if (free) {
      a.code = free
      a.is_circulation = false
      used.add(free)
      continue
    }

    const res = await createRoom(props.homeId, nm)
    a.code = res.code
    a.name = res.name
    a.is_circulation = false
    used.add(res.code)
    byName.set(nm, [...(byName.get(nm) || []), res.code])
    created += 1
  }

  if (created) {
    // 新房子的房间要进下拉：让父组件重拉一次
    emit('rooms-changed')
  }
}

/**
 * 鼠标可能在**画布外**松开（拖到弹窗边缘外），SVG 收不到 pointerup，
 * 状态就会卡在 drawing/drag 上 —— 下一次画框直接被忽略。
 * 挂在 window 上兜底；SVG 的 pointerup 先跑，跑完状态已清，这里就是空操作。
 */
function onWindowUp(e) {
  if (!drawing.value && !drag.value && !panning.value) return
  onUp(e)
}

onMounted(() => window.addEventListener('pointerup', onWindowUp))
onUnmounted(() => window.removeEventListener('pointerup', onWindowUp))

/** 8 个手柄的坐标（左上角 + 对应偏移）。尺寸随缩放反向变化，视觉大小恒定。 */
function handlesOf(a) {
  const c = handleSize.value / 2
  return [
    { k: 'nw', x: a.x - c, y: a.y - c },
    { k: 'n', x: a.x + a.w / 2 - c, y: a.y - c },
    { k: 'ne', x: a.x + a.w - c, y: a.y - c },
    { k: 'e', x: a.x + a.w - c, y: a.y + a.h / 2 - c },
    { k: 'se', x: a.x + a.w - c, y: a.y + a.h - c },
    { k: 's', x: a.x + a.w / 2 - c, y: a.y + a.h - c },
    { k: 'sw', x: a.x - c, y: a.y + a.h - c },
    { k: 'w', x: a.x - c, y: a.y + a.h / 2 - c },
  ]
}

// ---- 指派房间 / 改名 -------------------------------------------------------
/**
 * 房间**类型**（不是具体的某一间房）。
 *
 * 为什么改成类型：一套房可以有两个卫生间、三段走廊，让用户去"选已有房间"
 * 既选不过来也说不清。类型只用来给个默认名字 + 决定是不是交通空间，
 * 真正的房间在保存时按"名字"创建/复用。
 */
const TYPE_OPTIONS = [
  '客厅', '卧室', '厨房', '卫生间', '阳台',
  '餐厅', '书房', '玄关', '储物间', '其他房间', '走廊',
]
/** 选中它时不自动起名，让用户自己写（"其他房间"当名字没意义） */
const OTHER_TYPE = '其他房间' 
/** 这些类型算交通空间：不挂设备、可以重名、不建 rooms 记录 */
const CIRCULATION_TYPES = ['走廊', '通道', '过道']
const isCirculationType = (t) => CIRCULATION_TYPES.includes((t || '').trim())

/** 打开已有户型时，从名字猜类型（老数据没有 type 字段） */
function guessType(name, circ) {
  if (circ) return '走廊'
  const n = (name || '').trim()
  if (!n) return ''
  if (/(主卧|次卧|卧室|客卧|儿童房|老人房)/.test(n)) return '卧室'
  if (/(卫|浴|洗手间)/.test(n)) return '卫生间'
  if (n.includes('厨')) return '厨房'
  if (n.includes('阳')) return '阳台'
  if (n.includes('玄关')) return '玄关'
  if (n.includes('书')) return '书房'
  if (n.includes('餐')) return '餐厅'
  if (/(储|杂)/.test(n)) return '储物间'
  if (/(走廊|通道|过道)/.test(n)) return '走廊'
  if (n.includes('客')) return '客厅'
  // 叫不上类型的（工作区/茶室/电竞房…）统一归到"其他房间"，别留空
  return OTHER_TYPE
}

/**
 * 重名判断（只对非走廊生效）：
 *  - 同一张图上另一个非走廊框同名
 *  - 库里已有同名房间，且那间房**已经被别的框占用**（没被占用的会被复用，不算冲突）
 */
function nameConflict(a) {
  const nm = (a.name || '').trim()
  if (!nm || a.is_circulation) return ''
  if (
    areas.value.some(
      (x) => x !== a && !x.is_circulation && (x.name || '').trim() === nm,
    )
  ) {
    return nm
  }
  const used = new Set(
    areas.value.filter((x) => x !== a && x.code).map((x) => x.code),
  )
  const hit = (props.rooms || []).find((r) => r.name === nm && used.has(r.code))
  return hit ? nm : ''
}

const conflicts = computed(() =>
  [...new Set(areas.value.map(nameConflict).filter(Boolean))],
)

/** 生成下一个可用的走廊编码：`hallway_1`…；自己原来的编码优先复用。 */
function nextCirculationCode(prefer = '', self = null) {
  // 排除自己：否则把自己从"房间"改成"走廊"时，原来的 hallway 也算占用，编码会白白变号
  const used = new Set(
    areas.value.filter((x) => x !== self && x.code).map((x) => x.code),
  )
  if (prefer && !used.has(prefer)) return prefer
  let n = 1
  while (used.has(`hallway_${n}`)) n += 1
  return `hallway_${n}`
}

/**
 * 选房间类型。
 *
 * 名字沿用规则：**用户自己改过名字就不动它**，否则跟着类型走
 * （选「厨房」自动叫「厨房」，用户再改成「中厨」就保留）。
 */
function assignType(a, value) {
  const t = (value || '').trim()
  if (!t) return

  const wasCirc = a.is_circulation
  const oldType = a.type || ''
  const oldName = (a.name || '').trim()
  const followsType = !oldName || oldName === oldType || oldName === '走廊'

  a.type = t
  const circ = isCirculationType(t)

  if (circ) {
    if (!wasCirc) a.code = nextCirculationCode('', a) // 每段走廊一个独立编码
    a.is_circulation = true
    if (followsType) a.name = t
    return
  }

  if (wasCirc) a.code = '' // 从走廊改回房间：保存时按名字重建房间
  a.is_circulation = false

  if (t === OTHER_TYPE) {
    // 「其他房间」不自动起名：让用户直接写真实名字（茶室 / 电竞房 / 健身房…）
    if (followsType) a.name = ''
    const idx = areas.value.indexOf(a)
    if (idx >= 0) focusNameRow(idx)
    return
  }
  if (followsType) a.name = t
}

const assignedCount = computed(() => areas.value.filter((a) => a.code).length)
/** 还没命名的框：既没指定房间、也没打字 */
const unassignedCount = computed(
  () => areas.value.filter((a) => !a.code && !(a.name || '').trim()).length,
)

/**
 * 房间列表里存在、但这张图上没有框的房间。
 *
 * 三种情况都不该再提示，否则用户会看到"我已经画了/我刚删了，它还在催我画"：
 *  ① 有 `code` 的框 —— 正常已画出；
 *  ② 只写了名字、还没保存的框 —— 保存时会复用同名房间，此刻提示就是误报；
 *  ③ 已经标记「保存后删除」的房间 —— 保存完它就没了。
 */
const missingRooms = computed(() => {
  const deleting = new Set(pendingDeletes.value.map((x) => x.code))
  const drawnCodes = new Set()
  const drawnNames = new Set()
  for (const a of areas.value) {
    if (a.is_circulation) continue
    if (a.code) drawnCodes.add(a.code)
    const nm = (a.name || '').trim()
    if (nm) drawnNames.add(nm)
  }
  return (props.rooms || []).filter(
    (r) => !drawnCodes.has(r.code) && !deleting.has(r.code) && !drawnNames.has(r.name),
  )
})

/**
 * 移除一个"图上没有框"的房间。
 *
 * 之所以这里能直接删：这种房间在列表里没有对应的行，
 * 用户没有别的入口能删掉它（删框那条路要求先有框）。
 * 但删房间会连设备一起删，所以必须把设备数说清楚再确认。
 */
async function removeMissingRoom(r) {
  const n = r.device_count ?? 0
  try {
    await ElMessageBox.confirm(
      `房间「${r.name}」不在户型图上。` +
        (n
          ? `里面有 <b>${n} 台设备</b>，删除会连同它们和全部历史数据一起删掉。`
          : '里面没有设备。') +
        '<br/>不可恢复。',
      '移除房间',
      {
        type: 'warning',
        dangerouslyUseHTMLString: true,
        confirmButtonText: '移除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return // 取消
  }

  try {
    const res = await deleteRoom(props.homeId, r.code)
    ElMessage({ message: res.message, type: 'success', duration: 5000 })
    // 让顶栏重拉房间列表 → props.rooms 更新 → 这行提示自动消失
    emit('rooms-changed')
  } catch (e) {
    ElMessage({ message: `移除失败：${e.message}`, type: 'error', duration: 5000 })
  }
}

/**
 * 删除一个框。
 *
 * - 还没对应房间的框（刚画的）→ 直接移除
 * - 已经有房间的 → 确认后标记"待删除"，**保存时**再真删（房间 + 设备 + 历史数据）
 *
 * 为什么不在点删除时就调接口：用户可能删错了按「取消」，那房间就白没了。
 */
async function onRemove(i) {
  const a = areas.value[i]
  if (!a) return

  if (a.code && !a.is_circulation) {
    const room = (props.rooms || []).find((r) => r.code === a.code)
    const n = room?.device_count ?? 0
    try {
      await ElMessageBox.confirm(
        `会同时删除房间「${a.name || a.code}」` +
          (n ? `及其 <b>${n} 台设备</b>和它们的历史数据` : '（这个房间没有设备）') +
          '。<br/>点「保存」后生效，不可恢复。',
        '删除房间',
        {
          type: 'warning',
          dangerouslyUseHTMLString: true,
          confirmButtonText: '删除',
          cancelButtonText: '取消',
          confirmButtonClass: 'el-button--danger',
        },
      )
    } catch {
      return // 取消
    }
    if (!pendingDeletes.value.some((x) => x.code === a.code)) {
      pendingDeletes.value.push({ code: a.code, name: a.name || a.code, devices: n })
    }
  }
  removeAt(i)
}

function removeAt(i) {
  areas.value.splice(i, 1)
  if (activeIndex.value >= areas.value.length) activeIndex.value = areas.value.length - 1
  drag.value = null
}

// ---- 打开时同步当前几何 ----------------------------------------------------
watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    formName.value = props.homeName || ''
    const g = props.geometry
    // 打开时重置缩放：baseBox 与 viewBox 一起设，避免残留上一套房的倍率
    setBox(g?.view_box)
    background.value = g?.background || null
    areas.value = (g?.areas || []).map((a) => ({
      code: a.code,
      name: a.name || a.code,
      // 老数据没有 type：按名字猜一个，用户改一下就行
      type: a.type || guessType(a.name, a.is_circulation),
      x: a.x,
      y: a.y,
      w: a.w,
      h: a.h,
      is_circulation: !!a.is_circulation,
    }))
    activeIndex.value = -1
    drag.value = null
    pendingDeletes.value = []
  },
)

// ---- 上传底图 --------------------------------------------------------------
async function onFileChange(file) {
  const raw = file?.raw
  if (!raw) return
  uploading.value = true
  try {
    const out = await uploadFloorplanImage(raw, props.homeId)
    const g = out.floorplan
    setBox(g.view_box)
    background.value = g.background
    // 后端换底图时会清空旧框选（坐标系变了），这里跟着清
    areas.value = []
    ElMessage({ message: out.message, type: 'success', duration: 5000 })
    emit('saved', g)
  } catch (e) {
    ElMessage({ message: `上传失败：${e.message}`, type: 'error', duration: 5000 })
  } finally {
    uploading.value = false
  }
}

// ---- 删除底图 --------------------------------------------------------------
/**
 * 只删底图。
 *
 * ⚠️ **不清框、不改画布尺寸**：换图才需要清框（坐标系变了），
 *   只删图时坐标系原样保留，框的位置仍然正确。
 */
async function removeBg() {
  if (!background.value) return
  try {
    await ElMessageBox.confirm(
      '只会删掉底图，<b>框选与房间都保留</b>，画布尺寸也不变。',
      '删除底图',
      {
        type: 'warning',
        dangerouslyUseHTMLString: true,
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return // 取消
  }

  removingBg.value = true
  try {
    const out = await removeFloorplanImage(props.homeId)
    background.value = null
    ElMessage({ message: out.message, type: 'success', duration: 4500 })
    // 通知总览页重画（有底图/无底图的渲染方式不同）
    emit('saved', out.floorplan)
  } catch (e) {
    ElMessage({ message: `删除底图失败：${e.message}`, type: 'error', duration: 5000 })
  } finally {
    removingBg.value = false
  }
}

// ---- 清空 / 重置 ------------------------------------------------------------
/**
 * 清空图上的所有框。
 *
 * 只清"画在图上的框"，**不删房间、不回滚底图、也不改画布尺寸** ——
 * 房间还在列表里（底部会出现「图上没有这些房间」，可以逐个「移除」）。
 * 之所以放在本地不动后端：这是一次编辑操作，用户可能马上重画；
 * 点「取消」就该什么都不发生。
 */
async function clearFrames() {
  if (!areas.value.length) return
  try {
    await ElMessageBox.confirm(
      `会清掉图上的 <b>${areas.value.length} 个框</b>，房间本身不会删除。` +
        '<br/>清空后请重新画；不想保留的房间，可以在底部逐个点「移除」。',
      '清空所有框',
      {
        type: 'warning',
        dangerouslyUseHTMLString: true,
        confirmButtonText: '清空',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return // 取消
  }

  areas.value = []
  activeIndex.value = -1
  draft.value = null
  drawing.value = false
  drag.value = null
  ElMessage({ message: '已清空所有框，可以重新画了', type: 'success', duration: 2500 })
}

async function resetAll() {
  try {
    const out = await resetFloorplan(false, props.homeId)
    setBox(out.floorplan.view_box)
    background.value = null
    areas.value = (out.floorplan.areas || []).map((a) => ({ ...a }))
    activeIndex.value = -1
    ElMessage({ message: out.message, type: 'success', duration: 4500 })
    emit('saved', out.floorplan)
  } catch (e) {
    ElMessage({ message: `重置失败：${e.message}`, type: 'error', duration: 4500 })
  }
}

// ---- 保存 ------------------------------------------------------------------
async function save() {
  // 允许"删到没有框"——那是在删房间；但不允许空着保存（那等于把户型清没了）
  if (!areas.value.length && !pendingDeletes.value.length) {
    ElMessage({ message: '至少要画一个房间', type: 'warning' })
    return
  }
  const unnamed = areas.value.filter((a) => !a.code && !(a.name || '').trim())
  if (unnamed.length) {
    ElMessage({ message: `还有 ${unnamed.length} 个框没命名，点一下它的输入框打字即可`, type: 'warning' })
    return
  }
  if (conflicts.value.length) {
    ElMessage({
      message: `已有同名房间「${conflicts.value[0]}」，换个名字（走廊可以同名）`,
      type: 'warning',
      duration: 4000,
    })
    return
  }
  const newName = formName.value.trim()
  if (!newName) {
    ElMessage({ message: '套房名称不能为空', type: 'warning' })
    return
  }

  saving.value = true
  // 改名失败就别继续存户型 —— 否则会出现"户型存了、名字没改"的半成品状态
  let stage = 'rename'
  try {
    if (newName !== (props.homeName || '')) {
      const r = await renameHome(props.homeId, newName)
      ElMessage({ message: r.message, type: 'success', duration: 2500 })
      emit('renamed', r.name)
    }
    stage = 'floorsave'
    // 只写了名字的框，先按名字建/匹配房间（编码由后端生成）
    await resolveRooms()
    const out = await saveFloorplan(
      {
        // ⚠️ 必须是 baseBox（画布本体）：用 viewBox 会把"放大后看到的那一块"存成画布，
        //    下次打开整个户型被裁剪、底图也对不上。
        view_box: { ...baseBox.value },
        areas: areas.value.map((a) => ({
          code: a.code,
          name: (a.name || '').trim(),
          type: a.type || '',
          x: a.x,
          y: a.y,
          w: a.w,
          h: a.h,
          is_circulation: a.is_circulation,
        })),
      },
      props.homeId,
    )
    // ---- 删掉的房间：几何存好之后再删数据 ----
    // 顺序不能反：先删房间的话，这次提交的几何里还引用着它，校验会直接 400。
    const stillUsed = new Set(areas.value.map((a) => a.code).filter(Boolean))
    const targets = pendingDeletes.value.filter((x) => !stillUsed.has(x.code))
    const delMsgs = []
    let delFailed = false
    for (const t of targets) {
      try {
        const r = await deleteRoom(props.homeId, t.code)
        delMsgs.push(r.message)
      } catch (e) {
        delFailed = true
        delMsgs.push(`「${t.name}」删除失败：${e.message}`)
      }
    }
    pendingDeletes.value = []

    // 有删除时后端那句"注意：X 没画到图上"已经过期（X 刚被删了），只保留前半句
    const baseMsg =
      areas.value.length === 0
        ? '已清空户型'
        : out.message.split('；注意：')[0]
    ElMessage({
      message: [baseMsg, ...delMsgs].filter(Boolean).join('；'),
      type: delFailed ? 'warning' : 'success',
      duration: 6000,
    })
    emit('saved', out.floorplan)
    // 保存时可能新建/删除了房间 → 让顶栏重拉一次房间列表
    emit('rooms-changed')
    close()
  } catch (e) {
    ElMessage({
      message: `${stage === 'rename' ? '改名失败' : '保存失败'}：${e.message}`,
      type: 'error',
      duration: 5500,
    })
  } finally {
    saving.value = false
  }
}

function close() {
  emit('update:modelValue', false)
}
</script>

<style scoped>
.ed-head {
  display: flex;
  align-items: center;
  gap: 10px;
  /* 给右上角关闭按钮留位置 */
  padding-right: 24px;
}
.ed-head-title {
  flex: none;
  font-size: 17px;
  font-weight: 600;
  color: var(--c-ink);
}
.ed-head-label {
  flex: none;
  font-size: var(--fs-small);
  color: var(--c-ink-muted);
}
.ed-house-input {
  max-width: 360px;
}

.ed-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 8px;
  margin-bottom: 8px;
}
/* 提示文字允许被压缩（按钮不能），窄屏时先收提示 */
.ed-toolbar .hint {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.hint {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
  line-height: 1.5;
}
.hint b {
  color: var(--c-ink-soft);
  font-weight: 600;
}
.spacer {
  flex: 1;
}

/*
 * 弹窗整体锁在 90vh：画布用 flex 吃掉"标题 + 工具条 + 4 行列表 + 页脚"之外的全部高度。
 * 好处是屏幕越高画画区越大，同时永远不把页面撑出滚动条。
 */
:global(.plan-dialog) {
  height: min(90vh, 880px);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  margin-bottom: 0 !important;
}
:global(.plan-dialog .el-dialog__body) {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.ed-stage {
  flex: 1 1 auto;
  min-height: 160px;
  border: 1px solid var(--c-line);
  border-radius: var(--radius);
  background: var(--c-surface-soft);
  overflow: hidden;
}
.ed-svg {
  width: 100%;
  height: 100%;
  display: block;
  cursor: crosshair;
  touch-action: none; /* 触屏上拖拽画框时不要滚页面 */
  user-select: none; /* 拖拽时不要选中框里的文字 */
}
/* 中键平移中：给个抓手，让用户知道"手正按在画布上" */
.ed-svg--panning {
  cursor: grabbing;
}
.zoom-pct {
  min-width: 42px;
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
  text-align: center;
}
/* 底图只负责显示，绝不吃指针事件（否则浏览器会当"拖图片"处理） */
.ed-svg image {
  pointer-events: none;
  -webkit-user-drag: none;
}
.ed-canvas {
  fill: var(--c-surface);
  stroke: var(--c-line);
}

.ed-rect {
  fill: rgba(47, 111, 237, 0.14);
  stroke: var(--c-accent);
  stroke-width: 1.5;
  vector-effect: non-scaling-stroke;
  cursor: move;
}
.ed-rect--unassigned {
  fill: rgba(214, 138, 22, 0.14);
  stroke: var(--c-warn);
  stroke-dasharray: 4 3;
}
.ed-rect--active {
  fill: rgba(47, 111, 237, 0.24);
  stroke-width: 2.4;
}

.ed-handle {
  fill: var(--c-surface);
  stroke: var(--c-accent);
  stroke-width: 1.6;
  vector-effect: non-scaling-stroke;
}
.ed-handle--nw,
.ed-handle--se {
  cursor: nwse-resize;
}
.ed-handle--ne,
.ed-handle--sw {
  cursor: nesw-resize;
}
.ed-handle--n,
.ed-handle--s {
  cursor: ns-resize;
}
.ed-handle--e,
.ed-handle--w {
  cursor: ew-resize;
}

.ed-tag rect {
  fill: rgba(47, 111, 237, 0.92);
}
.ed-tag text {
  fill: #fff;
  font-weight: 600;
  letter-spacing: 0.2px;
}
.ed-tag--unassigned rect {
  fill: rgba(214, 138, 22, 0.95);
}

.ed-draft {
  fill: rgba(47, 111, 237, 0.1);
  stroke: var(--c-accent);
  stroke-width: 1.5;
  stroke-dasharray: 5 4;
  vector-effect: non-scaling-stroke;
  pointer-events: none;
}

.ed-list {
  margin-top: 8px;
}

/*
 * 固定 4 行高度（不是 max-height）：房间从 1 个长到 4 个的过程中，
 * 画布高度不能跟着变 —— 否则正在拖框时画布缩小，框会"跳"到别的位置，
 * 用户会以为坐标算错了。空列表时这块留白也无妨。
 */
.ed-rows {
  height: 140px; /* 4 × 35px 行高 */
  overflow-y: auto;
}
.ed-empty {
  padding: 16px;
  text-align: center;
  color: var(--c-ink-muted);
  font-size: var(--fs-small);
}
.ed-row {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 35px;
  box-sizing: border-box;
  padding: 0 8px;
  border-bottom: 1px solid var(--c-line-soft);
  font-size: var(--fs-small);
  cursor: pointer;
}
.ed-row--active {
  background: var(--c-accent-soft);
}
.ed-swatch {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  background: rgba(47, 111, 237, 0.35);
  border: 1px solid var(--c-accent);
  flex: 0 0 auto;
}
.ed-swatch--circ {
  background: rgba(120, 130, 148, 0.25);
  border-color: var(--c-ink-muted);
}
.ed-pos {
  color: var(--c-ink-muted);
  font-size: var(--fs-tiny);
  min-width: 132px;
}
.ed-select {
  width: 190px;
}
.ed-name {
  width: 190px;
}
/* 重名：输入框描红。行高固定 35px，放不下第二行提示，所以用边框 + title 兜住 */
.ed-name--dup :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--c-danger) inset;
}

.ed-summary {
  flex: 0 0 auto;
  margin-top: 6px;
  font-size: var(--fs-tiny);
  color: var(--c-ink-soft);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.warn {
  color: var(--c-warn);
}
/* 图上没有框的房间：带「移除」的地方，得让它看起来能点 */
.ed-missing {
  flex: 0 0 auto;
  margin-top: 4px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 6px;
  font-size: var(--fs-tiny);
  color: var(--c-warn);
}
.ed-missing-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 1px 4px 1px 6px;
  border: 1px dashed currentColor;
  border-radius: 4px;
}
.ed-missing-del {
  padding: 0 2px;
  border: none;
  background: transparent;
  color: inherit;
  font-size: inherit;
  text-decoration: underline;
  cursor: pointer;
}
.ed-missing-del:hover {
  color: var(--c-danger);
}
.ed-missing-hint {
  color: var(--c-ink-muted);
}
.footer-hint {
  float: left;
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
  line-height: 32px;
}
</style>
