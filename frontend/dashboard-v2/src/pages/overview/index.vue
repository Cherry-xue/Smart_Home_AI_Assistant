<template>
  <div class="overview">
    <!-- ==================== 主显示区 ==================== -->
    <!-- `page-fade`：切页时淡入淡出（规则见 styles/transitions.css） -->
    <section class="stage page-fade">
      <span class="stage-label">户型图</span>
      <div class="stage-tools">
        <span class="stage-badge">
          {{ dataModeText }}
          <template v-if="system.dataAnchor"> · 锚点 {{ fmtTime(system.dataAnchor) }}</template>
        </span>
        <el-tag v-if="floorplan?.is_custom" size="small" type="info" effect="plain">自定义</el-tag>
      </div>

      <div v-if="loadError" class="stage-error">
        数据加载失败：{{ loadError }}
      </div>

      <div class="stage-body">
        <!-- 左：详细数据（这间房没有可显示数据时整块隐藏） -->
        <RoomDataPanel v-if="showLeft" :key="`L-${selectedCode}`" class="slot slot--left" :room="selectedRoom" />

        <!-- 中：户型图 -->
        <div class="plan-slot">
          <FloorPlan
            :rooms-by-code="roomsByCode"
            :selected-code="selectedCode"
            :hover-code="hoverCode"
            :geometry="floorplan"
            @select="onSelect"
            @hover="onHover"
          />

          <!-- 自定义了底图但还没框房间：不要让人对着空图猜为什么点不动 -->
          <div v-if="noAreas" class="plan-hint">{{ noAreasHint }}</div>
        </div>

        <!-- 右：器件开关（这间房没有可控器件时整块隐藏） -->
        <RoomSwitchPanel
          v-if="showRight"
          :key="`R-${selectedCode}`"
          class="slot slot--right"
          :devices="controllableDevices"
          :caps-by-device="capsByDevice"
        />
      </div>
    </section>

    <!-- ==================== 下方菜单 ==================== -->
    <!-- `page-slide`：切页时离开下沉、进入升起（没有下方菜单的页面不标这个类，就不会有升降） -->
    <section class="bottom page-slide">
      <div class="card card--stat">
        <div class="card-title">在线设备</div>
        <div class="card-value tnum">
          {{ onlineCount }}<span class="card-unit">/ {{ totalCount }}</span>
        </div>
        <div class="card-foot muted">
          高风险 {{ deviceStats.high_risk ?? '—' }} 台 · 房间 {{ deviceStats.rooms ?? '—' }} 间
        </div>
      </div>

      <div class="card card--stat">
        <div class="card-title">近24h用电</div>
        <div class="card-value tnum">
          {{ num(energy?.total_kwh, 2) }}<span class="card-unit">kWh</span>
        </div>
        <div class="card-foot muted">
          ¥{{ num(energy?.total_cost, 2) }} · {{ energyNoteShort }}
        </div>
      </div>

      <div class="card card--scenes">
        <div class="card-title">用户收藏的已保存场景</div>
        <div v-if="favoriteScenes.length" class="scene-list">
          <button
            v-for="s in favoriteScenes.slice(0, 6)"
            :key="s.id"
            class="scene-chip"
            :title="s.description"
          >
            <span class="scene-name">{{ s.name }}</span>
            <span class="scene-actions tnum">{{ s.action_count }} 动作</span>
          </button>
          <span v-if="favoriteScenes.length > 6" class="muted scene-more">
            共 {{ favoriteScenes.length }} 个
          </span>
        </div>
        <p v-else class="empty">还没有收藏的场景 —— 去「场景编排」点卡片上的「收藏」</p>
      </div>
    </section>

    <!-- 户型编辑弹窗：上传底图 + 拖拽框选房间 -->
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  getDeviceCapabilities,
  getDevices,
  getEnergySummary,
  getFloorplan,
  getRooms,
  listScenes,
} from '@/api'
import { useHomeStore } from '@/stores/home'
import { useSystemStore } from '@/stores/system'
import { dataModeLabel, fmtTime, num } from '@/utils/format'
import FloorPlan from './FloorPlan.vue'
import RoomDataPanel from './RoomDataPanel.vue'
import RoomSwitchPanel from './RoomSwitchPanel.vue'

const system = useSystemStore()
const home = useHomeStore()

const rooms = ref([])
const devices = ref([])
const scenes = ref([])
const energy = ref(null)
const loadError = ref('')

/** 户型几何（`/api/floorplan`）。null = 还没拿到 → FloorPlan 回落内置默认户型 */
const floorplan = ref(null)

const selectedCode = ref(null)
const hoverCode = ref(null)
/** deviceId → capabilities 数组（懒加载，只取选中房间里的可控设备） */
const capsByDevice = reactive({})

// ---------------------------------------------------------------- 派生
const roomsByCode = computed(() => Object.fromEntries(rooms.value.map((r) => [r.code, r])))

const selectedRoom = computed(() => (selectedCode.value ? roomsByCode.value[selectedCode.value] : null))

const roomDevices = computed(() =>
  selectedCode.value ? devices.value.filter((d) => d.room_code === selectedCode.value) : [],
)

/** 可控器件 = 至少有一项可写能力（后端已给 writable_count，不用额外请求） */
const controllableDevices = computed(() => roomDevices.value.filter((d) => (d.writable_count ?? 0) > 0))

const showLeft = computed(() => {
  const r = selectedRoom.value
  if (!r) return false
  return (r.key_metrics?.length ?? 0) > 0 || (r.device_count ?? 0) > 0
})

const showRight = computed(() => controllableDevices.value.length > 0)

/** 这套房还没有可点的房间（没画过 / 只传了底图没框）—— 画布上点不动任何东西 */
const noAreas = computed(() => {
  const g = floorplan.value
  if (!g) return false
  return (g.source === 'custom' || g.source === 'empty') && (g.areas?.length ?? 0) === 0
})

/** 空画布时的提示：有底图就催他框选，没底图就从画第一个房间说起 */
const noAreasHint = computed(() =>
  floorplan.value?.background
    ? '已上传底图，但还没有框选房间 —— 点顶栏「编辑」在图上拖拽框选'
    : '这套房还没有房间 —— 点顶栏「编辑」，拖一个框写上名字，房间就建好了',
)

// ⚠️ 来自 /api/health 的 devices：键是 total/rooms/high_risk/online。
//    不要和 /api/devices 的 meta.summary（devices/rooms/high_risk/capabilities）混用。
/** 设备统计**按当前套房算**。
 *
 * ⚠️ 不能用 `system.deviceStats`（那份来自 /api/health，是**全库**统计）——
 *    切到二号房时底部还写着"32/32 在线"，那是别人家的设备。
 *    这里从本页已经拿到的 devices 里数，天然跟着套房走。
 */
const deviceStats = computed(() => {
  const list = devices.value
  return {
    total: list.length,
    online: list.filter((d) => d.online).length,
    high_risk: list.filter((d) => d.high_risk).length,
    rooms: rooms.value.length,
  }
})
const onlineCount = computed(() => deviceStats.value.online ?? '—')
const totalCount = computed(() => deviceStats.value.total ?? '—')

const dataModeText = computed(() => dataModeLabel(system.dataMode))

/** 总览只显示**收藏**的场景（收藏标记存在场景的 definition_json 里） */
const favoriteScenes = computed(() => scenes.value.filter((s) => s.favorite))

/** 后端已经给了 note 说明"金额由确定性代码算，未经模型估算"，这里取它的前半句 */
const energyNoteShort = computed(() => {
  const n = energy.value?.note
  return n ? n.split('，')[0] : '分时电价'
})

// ---------------------------------------------------------------- 交互
function onSelect(code) {
  selectedCode.value = selectedCode.value === code ? null : code
  hoverCode.value = null
  if (selectedCode.value) ensureCapabilities()
}

function onHover(code) {
  hoverCode.value = code
}

/** 懒加载选中房间里可控设备的能力档案（每台只需一次）。 */
async function ensureCapabilities() {
  const targets = controllableDevices.value.filter((d) => !capsByDevice[d.id])
  if (!targets.length) return
  const results = await Promise.allSettled(targets.map((d) => getDeviceCapabilities(d.id)))
  results.forEach((res, i) => {
    if (res.status === 'fulfilled') {
      capsByDevice[targets[i].id] = res.value?.capabilities ?? []
    } else {
      capsByDevice[targets[i].id] = []
    }
  })
}

// ---------------------------------------------------------------- 数据
let timer = null

async function loadAll() {
  try {
    const hid = home.currentId
    // 每个请求**各自兜底**：任何一个接口报错都不能让整页停在上一套房的数据上
    // （之前 /rooms 对空套房返回 404，Promise.all 整体失败，切过去还是旧户型）
    const [r, d, sc, e, fp] = await Promise.all([
      getRooms(hid).catch(() => ({ items: [] })),
      getDevices({ home_id: hid }).catch(() => ({ items: [] })),
      listScenes(hid).catch(() => []),
      getEnergySummary({ home_id: hid }).catch(() => null),
      // 户型几何：拿不到就保持 null → 回落默认户型，总览页不会因此变成空白
      getFloorplan(hid).catch(() => null),
    ])
    rooms.value = r?.items ?? []
    devices.value = d?.items ?? []
    scenes.value = Array.isArray(sc) ? sc : []
    energy.value = e
    floorplan.value = fp
    loadError.value = ''
    if (selectedCode.value) ensureCapabilities()
  } catch (err) {
    loadError.value = err.message
  }
}

/** 编辑器里保存/上传/重置之后，直接用返回的新几何刷新画布（不用再请求一次） */
/**
 * 户型在顶栏「编辑」里改过 → 重新取几何与房间。
 *
 * 为什么用 store 里的版本号：编辑器现在挂在 `AppShell`（顶栏按钮），
 * 与总览页不是父子，拿不到它的 `saved` 事件。版本号一变就重取，
 * 顺带把房间列表也刷新（保存时可能新建了房间）。
 */
watch(
  () => home.floorplanRev,
  async () => {
    const hid = home.currentId
    const [fp, rm] = await Promise.all([
      getFloorplan(hid).catch(() => null),
      getRooms(hid).catch(() => null),
    ])
    if (fp) floorplan.value = fp
    if (rm) rooms.value = rm?.items ?? []
    // 选中房间可能已经被删掉/换了位置 → 退出选中，避免面板指向不存在的区域
    if (
      selectedCode.value &&
      !(floorplan.value?.areas || []).some((a) => a.code === selectedCode.value)
    ) {
      selectedCode.value = null
    }
  },
)

/** 轮询只刷"会变"的部分：设备状态与房间读数。场景/能耗是慢变量，不用跟着刷。 */
async function refreshLive() {
  try {
    const hid = home.currentId
    const [r, d] = await Promise.all([getRooms(hid), getDevices({ home_id: hid })])
    rooms.value = r?.items ?? []
    devices.value = d?.items ?? []
    loadError.value = ''
  } catch (err) {
    loadError.value = err.message
  }
}

// 换套房 = 换一整套数据。选中房间、能力缓存都属于"上一套房"，必须清掉，
// 否则左侧面板会拿着旧房间的 code 去新套房里找数据。
watch(
  () => home.currentId,
  async () => {
    selectedCode.value = null
    hoverCode.value = null
    for (const k of Object.keys(capsByDevice)) delete capsByDevice[k]
    await loadAll()
  },
)

onMounted(async () => {
  await loadAll()
  timer = setInterval(refreshLive, 8000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.overview {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: var(--gap);
  gap: var(--gap);
}

/* ---------------- 主显示区 ---------------- */
.stage {
  position: relative;
  flex: 1;
  min-height: 0;
  border: 1px solid var(--c-line);
  display: flex;
  flex-direction: column;
}
.stage-label {
  position: absolute;
  top: 6px;
  left: 10px;
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
  z-index: 2;
}
.stage-badge {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
/* 右上角工具组：数据来源角标 + 自定义标记（编辑入口在顶栏） */
.stage-tools {
  position: absolute;
  top: 4px;
  right: 10px;
  display: flex;
  align-items: center;
  gap: 8px;
  z-index: 3;
}
.stage-error {
  margin: 26px 12px 0;
  padding: 8px 10px;
  font-size: var(--fs-small);
  color: var(--c-danger);
  border: 1px solid var(--c-danger);
}

.stage-body {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 10px;
  /* 上边留出"户型图"角标的空位；四周留白让房间放大时不贴边 */
  padding: 26px 12px 12px;
  overflow: hidden;
}

.slot {
  flex: 0 0 var(--w-panel);
  min-width: 0;
  animation: slot-in var(--dur) var(--ease);
}
@keyframes slot-in {
  from {
    opacity: 0;
    transform: translateX(0) scale(0.98);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.plan-slot {
  position: relative;   /* 给"还没框选房间"的提示做定位参照 */
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
}

.plan-hint {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  padding: 10px 16px;
  background: var(--c-surface);
  border: 1px dashed var(--c-warn);
  border-radius: var(--radius);
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
  pointer-events: none;
  z-index: 4;
}

/* ---------------- 下方菜单 ---------------- */
.bottom {
  flex: 0 0 118px;
  display: grid;
  grid-template-columns: 1fr 1fr 2.4fr;
  gap: var(--gap);
}
.card {
  border: 1px solid var(--c-line);
  padding: 12px 14px;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.card-title {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.card-value {
  margin-top: 6px;
  font-size: 26px;
  font-weight: 500;
  line-height: 1.1;
}
.card-unit {
  margin-left: 4px;
  font-size: var(--fs-small);
  font-weight: 400;
  color: var(--c-ink-muted);
}
.card-foot {
  margin-top: auto;
  font-size: var(--fs-tiny);
}

.card--scenes {
  overflow: hidden;
}
.scene-list {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-content: flex-start;
  overflow-y: auto;
}
.scene-chip {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  padding: 3px 8px;
  border: 1px solid var(--c-line);
  transition: border-color var(--dur) var(--ease), color var(--dur) var(--ease);
}
.scene-chip:hover {
  border-color: var(--c-accent);
  color: var(--c-accent);
}
.scene-name {
  font-size: var(--fs-small);
}
.scene-actions {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.scene-more {
  font-size: var(--fs-tiny);
  align-self: center;
}
</style>
