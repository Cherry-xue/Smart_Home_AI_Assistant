<template>
  <div class="page">
    <!-- ---------------------------------------------------- 筛选 -->
    <div class="panel">
      <div class="filter-row">
        <el-select v-model="filters.room" placeholder="全部房间" clearable style="width: 140px">
          <el-option v-for="r in roomOptions" :key="r.code" :label="r.name" :value="r.code" />
        </el-select>

        <el-select v-model="filters.type" placeholder="全部类型" clearable style="width: 150px">
          <el-option v-for="t in typeOptions" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>

        <el-input v-model="filters.keyword" placeholder="搜索设备名 / 编码" clearable style="width: 200px" />

        <el-checkbox v-model="filters.highRiskOnly">只看高风险</el-checkbox>
        <el-checkbox v-model="filters.onlineOnly">只看在线</el-checkbox>

        <div class="fr-right">
          <span class="fr-count">
            共 <b class="mono">{{ filtered.length }}</b> / {{ all.length }} 台
          </span>
          <el-button size="small" :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>
    </div>

    <!-- ---------------------------------------------------- 列表 -->
    <div class="panel">
      <el-table
        v-loading="loading"
        :data="filtered"
        size="small"
        height="calc(100vh - 300px)"
        @row-click="openDetail"
      >
        <el-table-column prop="name" label="设备" min-width="150">
          <template #default="{ row }">
            <div class="cell-dev">
              <el-icon v-if="row.high_risk" class="dev-lock" :size="14"><Lock /></el-icon>
              {{ row.name }}
              <span v-if="row.high_risk" class="dev-tag-risk">高风险</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="room_name" label="房间" width="90" />

        <el-table-column label="类型" width="110">
          <template #default="{ row }">{{ typeLabel(row.device_type) }}</template>
        </el-table-column>

        <el-table-column label="状态" width="88">
          <template #default="{ row }">
            <span class="tag-dot" :class="row.online ? 'dot-ok' : 'dot-off'" />
            {{ row.online ? '在线' : '离线' }}
          </template>
        </el-table-column>

        <el-table-column label="最新读数" min-width="230">
          <template #default="{ row }">
            <span v-for="(m, code) in topMetrics(row.latest)" :key="code" class="cell-metric">
              <span class="cm-code">{{ code }}</span>
              <span class="cm-val mono">{{ metricText(m.value, m.unit) }}</span>
            </span>
            <span v-if="!Object.keys(row.latest || {}).length" class="dim">—</span>
          </template>
        </el-table-column>

        <el-table-column label="数据时间" width="140">
          <template #default="{ row }">
            <span class="mono">{{ fmtDateTime(row.last_observed_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="能力" width="80" align="center">
          <template #default="{ row }">
            <span class="mono">{{ row.capability_count }}</span>
            <span class="dim"> / 可写 {{ row.writable_count }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="80" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="openDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- ---------------------------------------------------- 详情抽屉 -->
    <el-drawer v-model="drawer" :title="current?.name ?? '设备详情'" size="52%" direction="rtl">
      <div v-if="current" class="drawer-body">
        <el-descriptions :column="2" border size="small" class="dev-desc">
          <el-descriptions-item label="编码">{{ current.code }}</el-descriptions-item>
          <el-descriptions-item label="房间">{{ current.room_name }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ typeLabel(current.device_type) }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <span class="tag-dot" :class="current.online ? 'dot-ok' : 'dot-off'" />
            {{ current.online ? '在线' : '离线' }}
          </el-descriptions-item>
          <el-descriptions-item label="高风险">
            <el-tag :type="current.high_risk ? 'danger' : 'info'" size="small">
              {{ current.high_risk ? '是（永不自动执行）' : '否' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="允许自动执行">
            <el-tag :type="current.auto_exec_allowed ? 'success' : 'warning'" size="small">
              {{ current.auto_exec_allowed ? '允许' : '不允许' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="最新数据时间">{{ fmtDateTime(current.last_observed_at) }}</el-descriptions-item>
          <el-descriptions-item label="最后上报">{{ fmtDateTime(current.last_seen) }}</el-descriptions-item>
        </el-descriptions>

        <!-- 最新值 -->
        <div class="sec-title">当前值</div>
        <div class="latest-grid">
          <div v-for="(m, code) in current.latest" :key="code" class="latest-cell">
            <div class="lc-name">{{ code }}</div>
            <div class="lc-value mono">{{ metricText(m.value, m.unit) }}</div>
            <div class="lc-meta">{{ fmtTime(m.observed_at) }} · {{ m.quality }}</div>
          </div>
          <div v-if="!Object.keys(current.latest || {}).length" class="empty-box">暂无读数</div>
        </div>

        <!-- 能力档案 -->
        <div class="sec-title">
          能力档案
          <span class="dim">取值范围与是否可写由数据库档案决定，Agent 不得越界</span>
        </div>
        <el-table v-loading="loadingCaps" :data="capabilities" size="small" max-height="220">
          <el-table-column prop="display_name" label="能力" min-width="130" />
          <el-table-column prop="code" label="编码" min-width="130" />
          <el-table-column prop="value_type" label="类型" width="80" />
          <el-table-column label="取值范围" min-width="160">
            <template #default="{ row }">
              <span v-if="row.enum_values && row.enum_values.length">{{ row.enum_values.join(' / ') }}</span>
              <span v-else-if="row.value_min !== null || row.value_max !== null">
                {{ row.value_min ?? '-∞' }} ~ {{ row.value_max ?? '+∞' }} {{ row.unit ?? '' }}
              </span>
              <span v-else class="dim">不限</span>
            </template>
          </el-table-column>
          <el-table-column label="可写" width="70" align="center">
            <template #default="{ row }">
              <el-tag :type="row.writable ? 'success' : 'info'" size="small">{{ row.writable ? '是' : '否' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="风险" width="70" align="center">
            <template #default="{ row }">
              <el-tag :type="row.risk_level === 'high' ? 'danger' : row.risk_level === 'medium' ? 'warning' : 'info'" size="small">
                {{ row.risk_level }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>

        <!-- 历史曲线 -->
        <div class="sec-title">
          历史曲线
          <div class="curve-ctrl">
            <el-select v-model="curveMetric" size="small" style="width: 150px" @change="loadCurve">
              <el-option v-for="m in curveMetricOptions" :key="m" :label="m" :value="m" />
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
          <div v-else class="empty-box">该设备在这个时间窗内没有 {{ curveMetric }} 的读数</div>
          <div v-if="curveMeta" class="curve-meta">
            共 {{ curveMeta.row_count }} 行 · 质量 {{ JSON.stringify(curveMeta.quality_summary) }} ·
            陈旧 {{ curveMeta.stale_rows }} 行
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getDeviceCapabilities, getDeviceReadings, getDevices, getRooms } from '@/api'
import { useSystemStore } from '@/stores/system'
import { fmtDateTime, fmtTime, metricText, typeLabel } from '@/utils/format'
import EChart from '@/components/EChart.vue'

const route = useRoute()
const system = useSystemStore()

const all = ref([])
const rooms = ref([])
const loading = ref(false)

const filters = reactive({
  room: null,
  type: null,
  keyword: '',
  highRiskOnly: false,
  onlineOnly: false,
})

// 从总览页点房间卡片跳过来时带着 room_code
watch(
  () => route.query.room_code,
  (v) => {
    if (v) filters.room = v
  },
  { immediate: true },
)

const roomOptions = computed(() => rooms.value.map((r) => ({ code: r.code, name: r.name })))

const typeOptions = computed(() => {
  const set = [...new Set(all.value.map((d) => d.device_type))]
  return set.map((t) => ({ value: t, label: typeLabel(t) })).sort((a, b) => a.label.localeCompare(b.label))
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

async function load() {
  loading.value = true
  try {
    const [dev, rm] = await Promise.all([getDevices(), getRooms(1)])
    all.value = dev?.items ?? []
    rooms.value = rm?.items ?? []
  } catch (e) {
    all.value = []
  } finally {
    loading.value = false
  }
}

/** 表格里只放前两个读数，多了会挤爆列宽 */
function topMetrics(latest) {
  const entries = Object.entries(latest ?? {}).slice(0, 2)
  return Object.fromEntries(entries)
}

// ---------------------------------------------------------------- 抽屉
const drawer = ref(false)
const current = ref(null)
const capabilities = ref([])
const loadingCaps = ref(false)

const curveMetric = ref('')
const curveHours = ref(24)
const curveData = ref(null)
const loadingCurve = ref(false)

const curveMetricOptions = computed(() => Object.keys(current.value?.latest ?? {}))

const curveMeta = computed(() =>
  curveData.value
    ? {
        row_count: curveData.value.row_count,
        quality_summary: curveData.value.quality_summary,
        stale_rows: curveData.value.stale_rows,
      }
    : null,
)

async function openDetail(row) {
  current.value = row
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

  // 默认画第一个指标
  const keys = Object.keys(row.latest ?? {})
  curveMetric.value = keys[0] ?? ''
  if (curveMetric.value) loadCurve()
}

async function loadCurve() {
  if (!current.value || !curveMetric.value) return
  loadingCurve.value = true
  try {
    // 时间窗以【数据锚点】为终点：模拟器时间轴不在"现在"，用 now 会查不到
    const anchor = system.dataAnchor ? new Date(system.dataAnchor) : new Date()
    const from = new Date(anchor.getTime() - curveHours.value * 3600 * 1000)
    const iso = (d) => d.toISOString().slice(0, 19)
    curveData.value = await getDeviceReadings(current.value.id, {
      metric: [curveMetric.value],
      from: iso(from),
      to: iso(anchor),
    })
  } catch (e) {
    curveData.value = null
  } finally {
    loadingCurve.value = false
  }
}

const curveOption = computed(() => {
  const series = curveData.value?.metrics?.[curveMetric.value]
  if (!series || !series.points?.length) return null

  // bool 画成 0/1 才能连成线
  const toNum = (v) => {
    if (typeof v === 'boolean') return v ? 1 : 0
    if (typeof v === 'number') return v
    const n = Number(v)
    return Number.isNaN(n) ? null : n
  }

  const points = series.points
  const badIdx = new Set(points.map((p, i) => (p.quality !== 'good' ? i : -1)).filter((i) => i >= 0))

  return {
    backgroundColor: 'transparent',
    grid: { left: 50, right: 20, top: 26, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#0e1526',
      borderColor: '#2b3d5e',
      textStyle: { color: '#d8e4f5' },
      formatter: (ps) => {
        const p = ps[0]
        const raw = points[p.dataIndex]
        const q = raw?.quality ?? 'good'
        return `${p.axisValue}<br/>${p.seriesName}: <b>${raw?.value}</b>${series.unit ?? ''}<br/>质量: ${q}`
      },
    },
    xAxis: {
      type: 'category',
      data: points.map((p) => fmtTime(p.observed_at)),
      axisLine: { lineStyle: { color: '#2b3d5e' } },
      axisLabel: { color: '#8ba1c0', fontSize: 11 },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      name: series.unit ?? '',
      nameTextStyle: { color: '#5b739a' },
      axisLine: { lineStyle: { color: '#2b3d5e' } },
      axisLabel: { color: '#8ba1c0', fontSize: 11 },
      splitLine: { lineStyle: { color: 'rgba(43,61,94,0.35)' } },
    },
    series: [
      {
        name: curveMetric.value,
        type: 'line',
        smooth: true,
        symbolSize: (_, p) => (badIdx.has(p.dataIndex) ? 8 : 3),
        data: points.map((p) => toNum(p.value)),
        lineStyle: { width: 2, color: '#2ea8ff' },
        itemStyle: { color: '#2ea8ff' },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(46,168,255,0.28)' },
              { offset: 1, color: 'rgba(46,168,255,0)' },
            ],
          },
        },
        markPoint: badIdx.size
          ? {
              data: points.map((p, i) => (badIdx.has(i) ? { coord: [fmtTime(p.observed_at), toNum(p.value)] } : null)).filter(Boolean),
              itemStyle: { color: '#ff5c6c' },
              symbolSize: 10,
              label: { show: false },
            }
          : undefined,
      },
    ],
  }
})

onMounted(load)
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
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
  font-size: 12px;
  color: var(--text-sub);
}

.cell-dev {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #eaf2ff;
}

.dev-lock {
  color: #ff8b96;
}

.dev-tag-risk {
  padding: 0 5px;
  border: 1px solid #5a2430;
  border-radius: 3px;
  font-size: 10px;
  color: #ff8b96;
}

.cell-metric {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-right: 12px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #16223a;
  font-size: 11px;
}

.cm-code {
  color: var(--text-dim);
}

.cm-val {
  color: #d8e4f5;
}

.dim {
  color: var(--text-dim);
}

/* ---------------------------------------------------- 抽屉 */
.sec-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 18px 0 10px;
  font-size: 14px;
  font-weight: 600;
  color: #eaf2ff;
}

.sec-title::before {
  content: '';
  display: inline-block;
  width: 3px;
  height: 13px;
  border-radius: 2px;
  background: linear-gradient(180deg, #2ea8ff, #00e5c0);
}

.curve-ctrl {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.latest-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: 10px;
}

.latest-cell {
  padding: 10px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: #101a2e;
}

.lc-name {
  font-size: 11px;
  color: var(--text-dim);
}

.lc-value {
  margin: 4px 0;
  font-size: 18px;
  font-weight: 700;
  color: #fff;
}

.lc-meta {
  font-size: 10px;
  color: var(--text-dim);
}

.curve-meta {
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-dim);
}

.dev-desc {
  margin-bottom: 4px;
}
</style>
