<template>
  <!-- `page-fade`：切页时淡入淡出。本页没有下方菜单，所以不标 `page-slide` -->
  <div class="page page-fade">
    <!-- ==================== 筛选 ==================== -->
    <div class="panel filter-panel">
      <div class="filter-row">
        <el-select v-model="filters.room" placeholder="全部房间" clearable style="width: 130px">
          <el-option v-for="r in roomOptions" :key="r.code" :label="r.name" :value="r.code" />
        </el-select>

        <el-select v-model="filters.type" placeholder="全部类型" clearable style="width: 150px">
          <el-option v-for="t in typeOptions" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>

        <el-input v-model="filters.keyword" placeholder="搜索设备名 / 编码" clearable style="width: 190px" />

        <el-checkbox v-model="filters.highRiskOnly">只看高风险</el-checkbox>
        <el-checkbox v-model="filters.onlineOnly">只看在线</el-checkbox>

        <div class="fr-right">
          <span class="fr-count">
            共 <b class="tnum">{{ filtered.length }}</b> / {{ all.length }} 台
          </span>
          <el-button size="small" :loading="loading" @click="load">刷新</el-button>
          <el-button size="small" type="primary" @click="openAdd">添加设备</el-button>
        </div>
      </div>

      <!-- 已停用设备：只能从这里恢复（停用后它会从 /api/devices 消失） -->
      <div v-if="disabled.length" class="disabled-row">
        <span class="muted">已停用 {{ disabled.length }} 台：</span>
        <span v-for="d in disabled" :key="d.id" class="disabled-chip">
          {{ d.name }}
          <button class="link-btn" @click="restore(d)">恢复</button>
          <button class="link-btn link-btn--danger" @click="purge(d)">彻底删除</button>
        </span>
      </div>
    </div>

    <!-- ==================== 列表 ==================== -->
    <div class="panel list-panel">
      <el-table
        v-loading="loading"
        :data="filtered"
        size="small"
        height="100%"
        :row-class-name="rowClass"
        @row-click="openDetail"
      >
        <el-table-column prop="name" label="设备" min-width="160">
          <template #default="{ row }">
            <span class="cell-dev">
              {{ row.name }}
              <span v-if="row.high_risk" class="tag tag--high-risk">高风险</span>
            </span>
          </template>
        </el-table-column>

        <el-table-column prop="room_name" label="房间" width="80" />

        <el-table-column label="类型" width="120">
          <template #default="{ row }">{{ typeLabel(row.device_type) }}</template>
        </el-table-column>

        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <span class="dot" :class="row.online ? 'dot--ok' : 'dot--off'" />
            {{ row.online ? '在线' : '离线' }}
          </template>
        </el-table-column>

        <el-table-column label="最新读数" min-width="240">
          <template #default="{ row }">
            <span v-for="m in topMetrics(row.latest)" :key="m.code" class="cell-metric">
              <span class="cm-code">{{ m.label }}</span>
              <span class="cm-val tnum">{{ m.text }}</span>
            </span>
            <span v-if="!topMetrics(row.latest).length" class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="数据时间" width="140">
          <template #default="{ row }">
            <span class="tnum">{{ fmtDateTime(row.last_observed_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="能力" width="96" align="center">
          <template #default="{ row }">
            <span class="tnum">{{ row.capability_count }}</span>
            <span class="muted"> / 可写 {{ row.writable_count }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="118" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="openDetail(row)">详情</el-button>
            <el-button link size="small" @click.stop="disable(row)">停用</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- ==================== 详情抽屉 ==================== -->
    <el-drawer v-model="drawer" :title="current?.name ?? '设备详情'" size="54%" direction="rtl">
      <div v-if="current" class="drawer-body">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="编码">{{ current.code }}</el-descriptions-item>
          <el-descriptions-item label="房间">{{ current.room_name }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ typeLabel(current.device_type) }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <span class="dot" :class="current.online ? 'dot--ok' : 'dot--off'" />
            {{ current.online ? '在线' : '离线' }}
          </el-descriptions-item>
          <el-descriptions-item label="高风险">
            <span class="tag" :class="current.high_risk ? 'tag--high-risk' : ''">
              {{ current.high_risk ? '是（需手动确认后执行）' : '否' }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="允许自动执行">
            <span class="tag" :class="current.auto_exec_allowed ? 'tag--good' : 'tag--warn'">
              {{ current.auto_exec_allowed ? '允许' : '不允许' }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="最新数据时间">{{ fmtDateTime(current.last_observed_at) }}</el-descriptions-item>
          <el-descriptions-item label="最后上报">{{ fmtDateTime(current.last_seen) }}</el-descriptions-item>
        </el-descriptions>

        <!-- 设备控制（新增） -->
        <div class="sec-title">设备控制</div>
        <div v-if="controlChannelIssue" class="notice">
          控制通道不可用：{{ controlChannelIssue }}。下方控件暂时只读。
        </div>
        <DeviceControls :device="current" :capabilities="capabilities" :show-header="false" />

        <!-- 当前值 -->
        <div class="sec-title">当前值</div>
        <div class="latest-grid">
          <div v-for="m in latestList" :key="m.code" class="latest-cell">
            <div class="lc-name">
              {{ m.label }}
              <span v-if="m.writable" class="tag tag--accent">可写</span>
            </div>
            <div class="lc-value tnum">{{ m.text }}</div>
            <div class="lc-meta">{{ fmtTime(m.observed_at) }} · {{ qualityMeta(m.quality).label }}</div>
          </div>
          <div v-if="!latestList.length" class="empty">暂无读数</div>
        </div>

        <!-- 能力档案 -->
        <div class="sec-title">
          能力档案
          <span class="muted sec-hint">取值范围与是否可写由数据库档案决定，Agent 不得越界</span>
        </div>
        <el-table v-loading="loadingCaps" :data="capabilities" size="small" max-height="230">
          <el-table-column prop="display_name" label="能力" min-width="120" />
          <el-table-column prop="code" label="编码" min-width="130" />
          <el-table-column prop="value_type" label="类型" width="76" />
          <el-table-column label="取值范围" min-width="170">
            <template #default="{ row }">
              <span v-if="row.enum_values?.length">{{ row.enum_values.join(' / ') }}</span>
              <span v-else-if="row.value_min !== null || row.value_max !== null">
                {{ row.value_min ?? '-∞' }} ~ {{ row.value_max ?? '+∞' }} {{ row.unit ?? '' }}
              </span>
              <span v-else class="muted">不限</span>
            </template>
          </el-table-column>
          <el-table-column label="可写" width="64" align="center">
            <template #default="{ row }">
              <span class="tag" :class="row.writable ? 'tag--good' : ''">{{ row.writable ? '是' : '否' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="风险" width="72" align="center">
            <template #default="{ row }">
              <span
                class="tag"
                :class="
                  row.risk_level === 'high' ? 'tag--danger' : row.risk_level === 'medium' ? 'tag--warn' : ''
                "
              >
                {{ row.risk_level }}
              </span>
            </template>
          </el-table-column>
        </el-table>

        <!-- 历史曲线 -->
        <div class="sec-title">
          历史曲线
          <div class="curve-ctrl">
            <el-select v-model="curveMetric" size="small" style="width: 140px" @change="loadCurve">
              <el-option v-for="m in curveMetricOptions" :key="m.code" :label="m.label" :value="m.code" />
            </el-select>
            <el-radio-group v-model="curveHours" size="small" @change="loadCurve">
              <el-radio-button :value="6">6h</el-radio-button>
              <el-radio-button :value="24">24h</el-radio-button>
              <el-radio-button :value="72">72h</el-radio-button>
            </el-radio-group>
          </div>
        </div>
        <div v-loading="loadingCurve">
          <EChart v-if="curveOption" :option="curveOption" :height="240" />
          <div v-else class="empty chart-empty">
            该设备在这个时间窗内没有「{{ curveMetricLabel }}」的读数
          </div>
          <div v-if="curveMeta" class="curve-meta">
            {{ curveMeta.row_count }} 行
            <template v-if="curveMeta.downsampled">
              （降采样 {{ curveMeta.stride }}:1 → {{ curveMeta.returned_count }} 点）
            </template>
            · 质量 {{ qualityText }}
            <template v-if="curveMeta.stale_rows"> · 陈旧 {{ curveMeta.stale_rows }} 行</template>
          </div>
        </div>
      </div>
    </el-drawer>

    <!-- ==================== 添加设备 ==================== -->
    <el-dialog v-model="addOpen" title="添加设备" width="540px">
      <el-form label-width="84px" label-position="left">
        <el-form-item label="设备类型">
          <el-select v-model="addForm.presetKey" placeholder="从预设里选一个" style="width: 100%">
            <el-option v-for="p in presets" :key="p.key" :value="p.key" :label="p.label">
              <span>{{ p.label }}</span>
              <span class="opt-right muted">
                {{ p.capability_count }} 能力 · 可写 {{ p.writable_count }}
                <template v-if="p.high_risk"> · 高风险</template>
              </span>
            </el-option>
          </el-select>
        </el-form-item>

        <el-form-item label="放入房间">
          <el-select v-model="addForm.roomId" placeholder="选择房间" style="width: 100%">
            <el-option v-for="r in rooms" :key="r.room_id" :value="r.room_id" :label="r.name" />
          </el-select>
        </el-form-item>

        <!-- 新套房不再自动建房间，所以这里必须说清楚"房间从哪来"——
             否则用户只会看到一个空下拉框，不知道该先做什么 -->
        <p v-if="!rooms.length" class="dlg-warn">
          这套房还没有房间，所以没有可放的位置。<br />
          先去 <b>「房屋总览」</b> 点顶栏 <b>「编辑」</b>：在画布上拖一个框、写上房间名，
          保存后再回来添加设备。
        </p>

        <el-form-item label="设备名">
          <el-input v-model="addForm.name" placeholder="留空则自动命名为「房间名 + 类型名」" />
        </el-form-item>
      </el-form>

      <p v-if="selectedPreset?.note" class="dlg-note">{{ selectedPreset.note }}</p>

      <p v-if="selectedPreset?.high_risk" class="dlg-warn">
        ⚠️ 高风险设备：加进来之后，任何指令都只会先生成<b>待确认项</b>，需要手动确认后才下发。
        这条不可配置 —— 危险等级由设备类型决定，界面设置不了。
      </p>

      <p class="dlg-note muted">
        新增后设备立刻出现在列表里，但生成器约 <b>30 秒</b>后才会开始为它产数据
        （在那之前它会显示为在线但没有读数）。
      </p>

      <template #footer>
        <el-button @click="addOpen = false">取消</el-button>
        <el-button type="primary" :loading="adding" :disabled="!canAdd" @click="submitAdd">
          添加
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createDevice,
  getDeviceCapabilities,
  getDeviceReadings,
  getDevices,
  getRooms,
  listDevicePresets,
  listDisabledDevices,
  purgeDevice,
  setDeviceEnabled,
} from '@/api'
import DeviceControls from '@/components/DeviceControls.vue'
import EChart from '@/components/EChart.vue'
import { useHomeStore } from '@/stores/home'
import { useSystemStore } from '@/stores/system'
import { controlChannelIssue } from '@/utils/control'
import {
  fmtDateTime,
  fmtTime,
  metricLabel,
  metricText,
  qualityMeta,
  typeLabel,
} from '@/utils/format'
import { chartPalette } from '@/utils/theme'

const system = useSystemStore()

const all = ref([])
const rooms = ref([])
const home = useHomeStore()
// 初值 true：表格自带 v-loading 遮罩，但首帧若为 false 会先渲染
// el-table 的"暂无数据"，随后才盖遮罩 —— 看起来就是"先没有、再冒出来"。
const loading = ref(true)

const filters = reactive({
  room: null,
  type: null,
  keyword: '',
  highRiskOnly: false,
  onlineOnly: false,
})

const roomOptions = computed(() => rooms.value.map((r) => ({ code: r.code, name: r.name })))

const typeOptions = computed(() => {
  const set = [...new Set(all.value.map((d) => d.device_type))]
  return set
    .map((t) => ({ value: t, label: typeLabel(t) }))
    .sort((a, b) => a.label.localeCompare(b.label))
})

const filtered = computed(() => {
  const kw = filters.keyword.trim().toLowerCase()
  return all.value.filter((d) => {
    if (filters.room && d.room_code !== filters.room) return false
    if (filters.type && d.device_type !== filters.type) return false
    if (filters.highRiskOnly && !d.high_risk) return false
    if (filters.onlineOnly && !d.online) return false
    if (kw && !`${d.name}${d.code}`.toLowerCase().includes(kw)) return false
    return true
  })
})

const rowClass = ({ row }) => (row.high_risk ? 'row--risk' : '')

/** 表格里只放前两个读数，多了会挤爆列宽。显示中文名而不是英文码。 */
function topMetrics(latest) {
  return Object.entries(latest ?? {})
    .slice(0, 2)
    .map(([code, m]) => ({ code, label: metricLabel(code), text: metricText(code, m.value, m.unit) }))
}

/**
 * @param silent 静默刷新（不显示骨架屏）。用于"批准了一条指令之后立刻重取"——
 *   这时候数据几乎一定变了，没必要让整页先闪一下空白再回来。
 */
async function load({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    const hid = home.currentId
    const [dev, rm] = await Promise.all([
      getDevices({ home_id: hid }),
      getRooms(hid),
    ])
    all.value = dev?.items ?? []
    rooms.value = rm?.items ?? []
  } catch {
    all.value = []
  } finally {
    if (!silent) loading.value = false
  }
}

// 有人批准/驳回了指令 → 设备状态多半已经变了，立刻重取。
// 之前只能干等 8 秒的定时轮询，看起来像"批准了没反应"。
watch(() => system.deviceRevision, () => load({ silent: true }))

// ---------------------------------------------------------------- 预设 / 增删
const presets = ref([])
/** 已停用设备。⚠️ 它们不在 `all` 里（目录带 enabled=1 过滤），只能单独查 */
const disabled = ref([])

const addOpen = ref(false)
const adding = ref(false)
const addForm = reactive({ presetKey: '', roomId: null, name: '' })

const selectedPreset = computed(
  () => presets.value.find((p) => p.key === addForm.presetKey) ?? null,
)
const canAdd = computed(() => !!addForm.presetKey && !!addForm.roomId)

async function loadPresets() {
  try {
    presets.value = (await listDevicePresets())?.items ?? []
  } catch {
    presets.value = []
  }
}

async function loadDisabled() {
  try {
    disabled.value = (await listDisabledDevices())?.items ?? []
  } catch {
    disabled.value = []
  }
}

function openAdd() {
  addForm.presetKey = ''
  addForm.roomId = rooms.value[0]?.room_id ?? null
  addForm.name = ''
  addOpen.value = true
}

async function submitAdd() {
  if (!canAdd.value) return
  adding.value = true
  try {
    const out = await createDevice({
      presetKey: addForm.presetKey,
      roomId: addForm.roomId,
      name: addForm.name.trim() || null,
    })
    // 消息直接用后端给的 —— 它已经说明了"生成器约 30 秒后才产数据"和高风险提示
    ElMessage({ message: out.message, type: 'success', duration: 6000 })
    addOpen.value = false
    await load()
  } catch (e) {
    ElMessage({ message: `添加失败：${e.message}`, type: 'error', duration: 4500 })
  } finally {
    adding.value = false
  }
}

async function disable(row) {
  try {
    const out = await setDeviceEnabled(row.id, false)
    ElMessage({ message: out.message, type: 'success', duration: 4000 })
    if (currentId.value === row.id) drawer.value = false
    await Promise.all([load(), loadDisabled()])
  } catch (e) {
    ElMessage({ message: `停用失败：${e.message}`, type: 'error', duration: 4500 })
  }
}

async function restore(d) {
  try {
    const out = await setDeviceEnabled(d.id, true)
    ElMessage({ message: out.message, type: 'success', duration: 4000 })
    await Promise.all([load(), loadDisabled()])
  } catch (e) {
    ElMessage({ message: `恢复失败：${e.message}`, type: 'error', duration: 4500 })
  }
}

/**
 * 彻底删除（不可逆）。
 *
 * ⚠️ 确认框里必须把"会失去什么"一条条写清楚，而不是只问"确定删除吗"。
 *    这是本项目唯一会销毁审计数据的入口，用户得知道自己在放弃什么。
 *    删完还要把后端的**逐表行数和受影响场景**原样展示出来。
 */
async function purge(d) {
  try {
    await ElMessageBox.confirm(
      `确定要彻底删除「${d.name}」吗？这是不可逆的。\n\n` +
        '删除后会永久失去：\n' +
        '· 它的全部历史读数（曲线、能耗统计都会少掉这部分）\n' +
        '· 它的指令记录，包括高风险动作的人工确认留痕\n' +
        '· 引用它的场景里，针对它的那些动作会失效\n\n' +
        '如果只是暂时不用，点「停用」就够了 —— 历史数据都会保留，随时能恢复。',
      '彻底删除设备',
      {
        type: 'warning',
        confirmButtonText: '确认彻底删除',
        cancelButtonText: '算了',
        confirmButtonClass: 'el-button--danger',
        // 多行文本要保留换行，Element Plus 默认会把 \n 当空白处理
        customClass: 'purge-confirm',
      },
    )
  } catch {
    return // 用户取消
  }

  try {
    const out = await purgeDevice(d.id)
    // 消息较长（含逐表行数与受影响场景），给足时间和关闭按钮
    ElMessage({ message: out.message, type: 'success', duration: 9000, showClose: true })
    await Promise.all([load(), loadDisabled()])
  } catch (e) {
    ElMessage({ message: `删除失败：${e.message}`, type: 'error', duration: 7000, showClose: true })
  }
}

// ---------------------------------------------------------------- 抽屉
const drawer = ref(false)
/** ⚠️ 存 id 而不是对象：轮询会把 all 整个换掉，存对象会让抽屉里的值停在打开那一刻 */
const currentId = ref(null)
const current = computed(() => all.value.find((d) => d.id === currentId.value) ?? null)

const capabilities = ref([])
const loadingCaps = ref(false)

const curveMetric = ref('')
const curveHours = ref(24)
const curveData = ref(null)
const loadingCurve = ref(false)

/** ⚠️ 来自 /api/devices 的 latest 是 {metric: {value, unit, ...}}，契约别记错 */
const latestEntries = computed(() => Object.entries(current.value?.latest ?? {}))

const latestList = computed(() =>
  latestEntries.value.map(([code, m]) => {
    const cap = capabilities.value.find((c) => c.code === code)
    return {
      code,
      label: metricLabel(code),
      text: metricText(code, m.value, m.unit),
      observed_at: m.observed_at,
      quality: m.quality,
      writable: !!cap?.writable,
    }
  }),
)

const curveMetricOptions = computed(() =>
  latestEntries.value.map(([code]) => ({ code, label: metricLabel(code) })),
)

const curveMetricLabel = computed(() => metricLabel(curveMetric.value))

const curveMeta = computed(() => curveData.value ?? null)

/** 质量分布：`{good: 3429, bad: 47}` → "好 3429 / 差 47"（别把 JSON 原样打到界面上） */
const qualityText = computed(() => {
  const q = curveMeta.value?.quality_summary || {}
  const parts = []
  if (q.good) parts.push(`好 ${q.good}`)
  if (q.suspect) parts.push(`疑 ${q.suspect}`)
  if (q.bad) parts.push(`差 ${q.bad}`)
  return parts.length ? parts.join(' / ') : '—'
})

async function openDetail(row) {
  currentId.value = row.id
  drawer.value = true
  curveData.value = null
  capabilities.value = []

  loadingCaps.value = true
  try {
    const d = await getDeviceCapabilities(row.id)
    capabilities.value = d?.capabilities ?? []
  } finally {
    loadingCaps.value = false
  }

  curveMetric.value = Object.keys(row.latest ?? {})[0] ?? ''
  if (curveMetric.value) loadCurve()
}

async function loadCurve() {
  if (!current.value || !curveMetric.value) return
  loadingCurve.value = true
  try {
    // ⚠️ 时间窗以【数据锚点】为终点：模拟器时间轴可能领先/落后真实时钟，
    //    用 now 会查不到或者查到未来。且必须传 metric —— 否则全量 24h 点数过多。
    const anchor = system.dataAnchor ? new Date(system.dataAnchor) : new Date()
    const from = new Date(anchor.getTime() - curveHours.value * 3600 * 1000)
    // ⚠️ 必须发**本地时间**字符串，不能用 `toISOString()`：
    //    后端把这个不带时区的字符串按本地时间解析，而 toISOString 会转成 UTC ——
    //    整体偏移 8 小时，6h/24h 窗口直接落到空区间（实测 row_count=0，图是空的），
    //    只有 72h 窗口够宽才碰巧还盖着数据。能耗页的 isoLocal 也是同理。
    const iso = (d) => {
      const p2 = (n) => String(n).padStart(2, '0')
      return (
        `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}` +
        `T${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`
      )
    }
    curveData.value = await getDeviceReadings(current.value.id, {
      metric: [curveMetric.value],
      from: iso(from),
      to: iso(anchor),
    })
  } catch {
    curveData.value = null
  } finally {
    loadingCurve.value = false
  }
}

const curveOption = computed(() => {
  const series = curveData.value?.metrics?.[curveMetric.value]
  if (!series?.points?.length) return null

  const pal = chartPalette()
  const toNum = (v) => {
    if (typeof v === 'boolean') return v ? 1 : 0
    if (typeof v === 'number') return v
    const n = Number(v)
    return Number.isNaN(n) ? null : n
  }

  const points = series.points
  const badIdx = new Set(points.map((p, i) => (p.quality !== 'good' ? i : -1)).filter((i) => i >= 0))
  const labels = points.map((p) => fmtTime(p.observed_at))

  return {
    backgroundColor: 'transparent',
    grid: { left: 52, right: 20, top: 26, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: pal.tipBg,
      borderColor: pal.tipBorder,
      textStyle: { color: '#1a1a1a' },
      formatter: (ps) => {
        const p = ps[0]
        const raw = points[p.dataIndex]
        return `${p.axisValue}<br/>${curveMetricLabel.value}: <b>${raw?.value}</b>${series.unit ?? ''}<br/>质量: ${qualityMeta(raw?.quality).label}`
      },
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: pal.axis } },
      axisLabel: { color: pal.label, fontSize: 11 },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      name: series.unit ?? '',
      nameTextStyle: { color: pal.label },
      axisLine: { lineStyle: { color: pal.axis } },
      axisLabel: { color: pal.label, fontSize: 11 },
      splitLine: { lineStyle: { color: pal.grid } },
    },
    series: [
      {
        name: curveMetricLabel.value,
        type: 'line',
        smooth: true,
        showSymbol: points.length <= 120,
        data: points.map((p) => toNum(p.value)),
        lineStyle: { width: 1.6, color: pal.line },
        itemStyle: { color: pal.line },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: pal.areaTop },
              { offset: 1, color: pal.areaBottom },
            ],
          },
        },
        markPoint: badIdx.size
          ? {
              symbolSize: 8,
              itemStyle: { color: pal.bad },
              label: { show: false },
              data: points
                .map((p, i) => (badIdx.has(i) ? { coord: [labels[i], toNum(p.value)] } : null))
                .filter(Boolean),
            }
          : undefined,
      },
    ],
  }
})

// ---------------------------------------------------------------- 轮询
let timer = null
// 换套房 = 换一套设备清单：抽屉里可能正开着上一套房的设备，先关掉
watch(
  () => home.currentId,
  async () => {
    detail.value = null
    await Promise.all([load(), loadDisabled()])
  },
)

onMounted(async () => {
  await Promise.all([load(), loadDisabled(), loadPresets()])
  // 轮询让"下发指令 → 设备状态变化"能在界面上真的看到
  timer = setInterval(load, 8000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.page {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  padding: var(--gap);
}

.panel {
  border: 1px solid var(--c-line);
}
.filter-panel {
  flex: 0 0 auto;
  padding: 10px 12px;
}
.list-panel {
  flex: 1;
  min-height: 0;
  padding: 2px 0;
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.fr-right {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
}
.fr-count {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}

/* 已停用设备那一行 */
.disabled-row {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--c-line);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: var(--fs-small);
}
.disabled-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 1px 8px;
  border: 1px solid var(--c-line);
  color: var(--c-ink-soft);
}
.link-btn {
  font-size: var(--fs-small);
  color: var(--c-accent);
  text-decoration: underline;
}
.link-btn--danger {
  color: var(--c-danger);
}

/* 下拉选项右侧的能力计数 */
.opt-right {
  float: right;
  font-size: var(--fs-tiny);
}

/* 对话框里的说明与警告 */
.dlg-note {
  margin: 0 0 8px;
  font-size: var(--fs-small);
  line-height: 1.6;
}
.dlg-warn {
  margin: 0 0 8px;
  padding: 8px 10px;
  font-size: var(--fs-small);
  line-height: 1.6;
  color: var(--c-high-risk);
  border: 1px solid var(--c-high-risk);
}

.cell-dev {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.cell-metric {
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
  margin-right: 12px;
  padding: 1px 6px;
  border: 1px solid var(--c-line-soft);
  background: var(--c-surface-soft);
  font-size: var(--fs-tiny);
}
.cm-code {
  color: var(--c-ink-muted);
}
.cm-val {
  color: var(--c-ink);
}

.dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 5px;
  border-radius: 50%;
  vertical-align: middle;
}
.dot--ok {
  background: var(--c-good);
}
.dot--off {
  background: var(--c-ink-muted);
}

/* ---------------------------------------------------- 抽屉 */
.drawer-body {
  padding-bottom: 20px;
}

.sec-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 22px 0 10px;
  padding-top: 14px;
  border-top: 1px solid var(--c-line);
  font-size: 15px;
  font-weight: 500;
}
.sec-hint {
  font-size: var(--fs-tiny);
  font-weight: 400;
}

.notice {
  margin-bottom: 10px;
  padding: 8px;
  font-size: var(--fs-tiny);
  color: var(--c-warn);
  border: 1px solid var(--c-warn);
}

.curve-ctrl {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.latest-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}
.latest-cell {
  padding: 10px 12px;
  border: 1px solid var(--c-line);
  background: var(--c-surface-soft);
}
.lc-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.lc-value {
  margin: 5px 0 3px;
  font-size: 19px;
}
.lc-meta {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}

.chart-empty {
  padding: 40px 0;
  text-align: center;
  border: 1px dashed var(--c-line);
}
.curve-meta {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.curve-down {
  color: var(--c-warn);
  border-color: var(--c-warn);
}
</style>

<style>
/* 高风险行给一点底色提示（scoped 覆盖不到 el-table 内部，所以用全局 + 明确类名） */
.el-table .row--risk > td.el-table__cell {
  background: #fdf7f0;
}

/* 彻底删除的确认框：保留确认文案里的换行。
   ⚠️ message box 的正文默认把 \n 当空白折叠，那几条"会失去什么"就会挤成一坨。 */
.purge-confirm .el-message-box__message {
  white-space: pre-line;
  line-height: 1.7;
}
</style>
