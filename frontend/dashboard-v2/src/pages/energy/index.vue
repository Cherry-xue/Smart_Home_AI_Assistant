<template>
  <div class="page page-fade">
    <!-- ==================== 顶部 ==================== -->
    <div class="panel top-strip">
      <div class="ts-left">
        <span class="ts-title">能耗</span>
        <span class="muted tiny">
          {{ fmtDateTime(summary.from) }} → {{ fmtDateTime(summary.to) }}
        </span>
      </div>
      <div class="ts-right">
        <el-radio-group v-model="range" size="small" @change="load">
          <el-radio-button :value="24">近 24 小时</el-radio-button>
          <el-radio-button :value="72">近 3 天</el-radio-button>
          <el-radio-button :value="168">近 7 天</el-radio-button>
        </el-radio-group>
        <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <!-- ==================== 指标卡 ==================== -->
    <div class="kpi-row">
      <div class="kpi panel">
        <span class="kpi-label">总用电量</span>
        <span class="kpi-value tnum">{{ num(summary.total_kwh, 2) }}<i>kWh</i></span>
      </div>
      <div class="kpi panel">
        <span class="kpi-label">估算电费</span>
        <span class="kpi-value tnum">¥{{ num(summary.total_cost, 2) }}</span>
      </div>
      <div class="kpi panel">
        <span class="kpi-label">峰段占比</span>
        <span class="kpi-value tnum">{{ pct(peak?.share) }}</span>
        <span class="kpi-note tiny muted">峰段 {{ num(peak?.kwh, 2) }} kWh</span>
      </div>
      <div class="kpi panel">
        <span class="kpi-label">谷段占比</span>
        <span class="kpi-value tnum">{{ pct(valley?.share) }}</span>
        <span class="kpi-note tiny muted">谷段 {{ num(valley?.kwh, 2) }} kWh</span>
      </div>
    </div>

    <div class="grid">
      <!-- ==================== 左：逐小时 ==================== -->
      <section class="panel">
        <header class="sec-head">
          <h3 class="sec-title">逐小时用电</h3>
          <span class="muted tiny">{{ hourlyText }}</span>
        </header>
        <!-- 图表加载中：占位块高度与图表一致，数据到了原地替换、不跳版 -->
        <EChart v-if="hourlyOption" :option="hourlyOption" :height="248" />
        <SkeletonList v-else-if="loading" :rows="1" :bar-h="248" />
        <p v-else class="empty">该时间窗内没有用能数据</p>
      </section>

      <!-- ==================== 右：分时段 + 高耗设备 ==================== -->
      <div class="col-right">
        <section class="panel">
          <header class="sec-head">
            <h3 class="sec-title">分时电价</h3>
            <span class="muted tiny">峰 / 平 / 谷</span>
          </header>
          <div class="period-list">
            <div v-for="p in summary.by_period" :key="p.period" class="period-row">
              <span class="p-name" :class="`p-name--${p.period}`">{{ periodLabel(p.period) }}</span>
              <span class="p-bar">
                <i :style="{ width: `${Math.min((p.share || 0) * 100, 100)}%` }" :class="`p-fill--${p.period}`" />
              </span>
              <span class="p-val tnum">{{ num(p.kwh, 2) }} kWh</span>
              <span class="p-price tnum muted">
                {{ p.price === null ? '—' : `¥${num(p.price, 2)}` }}/kWh
              </span>
              <span class="p-cost tnum">¥{{ num(p.cost, 2) }}</span>
            </div>
            <SkeletonList v-if="loading && !summary.by_period.length" :rows="4" />
            <p v-else-if="!summary.by_period.length" class="empty">暂无数据</p>
          </div>
        </section>

        <section class="panel">
          <header class="sec-head">
            <h3 class="sec-title">高耗设备</h3>
            <span class="muted tiny">按用电量排序</span>
          </header>
          <div class="dev-list">
            <div v-for="(d, i) in topDevices" :key="d.device_id" class="dev-row">
              <span class="dev-rank tnum">{{ i + 1 }}</span>
              <span class="dev-name">
                {{ d.name }}
                <i class="tiny muted">{{ d.room_name || '—' }}</i>
              </span>
              <span class="dev-kwh tnum">{{ num(d.kwh, 2) }} kWh</span>
              <span class="dev-cost tnum">¥{{ num(d.cost, 2) }}</span>
            </div>
            <SkeletonList v-if="loading && !topDevices.length" :rows="5" />
            <p v-else-if="!topDevices.length" class="empty">暂无数据</p>
          </div>
        </section>
      </div>
    </div>

    <!-- ==================== 事实陈述 ==================== -->
    <section v-if="summary.observations?.length" class="panel">
      <header class="sec-head">
        <h3 class="sec-title">数据说明</h3>
      </header>
      <ul class="obs-list">
        <li v-for="(o, i) in summary.observations" :key="i">{{ o }}</li>
      </ul>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import EChart from '@/components/EChart.vue'
import SkeletonList from '@/components/SkeletonList.vue'
import { getEnergySummary } from '@/api'
import { fmtDateTime, num, periodLabel } from '@/utils/format'
import { useHomeStore } from '@/stores/home'
import { cssVar } from '@/utils/theme'

const home = useHomeStore()
// 首帧就是加载态：否则淡入那 300ms 里显示的是"该时间窗内没有用能数据"
const loading = ref(true)
const range = ref(24)
const summary = ref({
  from: null,
  to: null,
  total_kwh: 0,
  total_cost: 0,
  by_period: [],
  by_device: [],
  hourly: [],
  observations: [],
})

const peak = computed(() => summary.value.by_period.find((p) => p.period === 'peak'))
const valley = computed(() => summary.value.by_period.find((p) => p.period === 'valley'))

const topDevices = computed(() =>
  [...(summary.value.by_device || [])].sort((a, b) => b.kwh - a.kwh).slice(0, 8),
)

const pct = (v) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`)

const hourlyText = computed(() => {
  const list = summary.value.hourly || []
  if (!list.length) return ''
  const max = list.reduce((m, h) => Math.max(m, h.kwh), 0)
  return `共 ${list.length} 个时段 · 峰值 ${num(max, 2)} kWh`
})

const hourlyOption = computed(() => {
  const list = summary.value.hourly || []
  if (!list.length) return null
  const accent = cssVar('--c-accent', '#2f6fed')
  const axis = cssVar('--c-chart-axis', '#b9c0cc')
  const grid = cssVar('--c-chart-grid', '#eceff4')
  const ink = cssVar('--c-chart-label', '#6b7280')
  return {
    grid: { left: 44, right: 16, top: 16, bottom: 30 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v) => `${num(v, 3)} kWh`,
    },
    xAxis: {
      type: 'category',
      data: list.map((h) => `${String(h.hour).padStart(2, '0')}:00`),
      axisLine: { lineStyle: { color: axis } },
      axisTick: { show: false },
      axisLabel: { color: ink, fontSize: 10, interval: 1 },
    },
    yAxis: {
      type: 'value',
      name: 'kWh',
      nameTextStyle: { color: ink, fontSize: 10 },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: grid } },
      axisLabel: { color: ink, fontSize: 10 },
    },
    series: [
      {
        type: 'bar',
        data: list.map((h) => h.kwh),
        barMaxWidth: 18,
        itemStyle: { color: accent, borderRadius: [2, 2, 0, 0] },
      },
    ],
  }
})

async function load() {
  loading.value = true
  try {
    const data = await getEnergySummary({ home_id: home.currentId })
    // 后端缺省窗口是 24 小时（以数据锚点为基准）。这里自己按选择的跨度收窄：
    // 直接把锚点作为 to、往前推 range 小时。锚点由返回体给出，避免用本机时钟。
    if (range.value !== 24 && data?.to) {
      const to = new Date(data.to)
      const from = new Date(to.getTime() - range.value * 3600 * 1000)
      summary.value = await getEnergySummary({
        home_id: home.currentId,
        from: isoLocal(from),
        to: isoLocal(to),
      })
    } else {
      summary.value = data
    }
  } catch (e) {
    ElMessage({ message: `能耗加载失败：${e.message}`, type: 'error', duration: 4500 })
  } finally {
    loading.value = false
  }
}

/** 本地时区的 ISO（不带 Z）：后端按本地时间存，带 Z 会被当成 UTC 再换算一次 */
function isoLocal(d) {
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}

watch(() => home.currentId, load)
onMounted(load)
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  padding: var(--pad);
  height: 100%;
  min-height: 0;
  overflow: auto;
}
.panel {
  background: var(--c-surface);
  border: 1px solid var(--c-line);
  border-radius: var(--radius);
  padding: var(--pad);
}

.top-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--gap);
}
.ts-left {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.ts-title {
  font-size: var(--fs-title);
  font-weight: 600;
}
.ts-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--gap);
}
.kpi {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.kpi-label {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.kpi-value {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.1;
}
.kpi-value i {
  font-style: normal;
  font-size: var(--fs-small);
  font-weight: 400;
  color: var(--c-ink-muted);
  margin-left: 8px;
}

.grid {
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr);
  gap: var(--gap);
}
.col-right {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
}

.sec-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.sec-title {
  margin: 0;
  font-size: var(--fs-body);
  font-weight: 600;
}

/* ---------------- 分时段 ---------------- */
.period-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.period-row {
  display: grid;
  grid-template-columns: 46px minmax(0, 1fr) 92px 96px 74px;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-small);
}
.p-name {
  font-size: var(--fs-tiny);
  padding: 0 6px;
  border-radius: 3px;
  text-align: center;
  border: 1px solid var(--c-line);
  color: var(--c-ink-soft);
}
.p-name--peak {
  border-color: var(--c-danger);
  color: var(--c-danger);
}
.p-name--valley {
  border-color: var(--c-good);
  color: var(--c-good);
}
.p-bar {
  height: 6px;
  border-radius: 3px;
  background: var(--c-surface-soft);
  overflow: hidden;
}
.p-bar i {
  display: block;
  height: 100%;
  background: var(--c-accent);
}
.p-fill--peak {
  background: var(--c-danger) !important;
}
.p-fill--valley {
  background: var(--c-good) !important;
}
.p-val,
.p-cost {
  text-align: right;
}
.p-price {
  text-align: right;
  font-size: var(--fs-tiny);
}

/* ---------------- 设备 ---------------- */
.dev-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.dev-row {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) 96px 76px;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-small);
}
.dev-rank {
  color: var(--c-ink-muted);
  font-size: var(--fs-tiny);
}
.dev-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dev-name i {
  font-style: normal;
  margin-left: 6px;
}
.dev-kwh,
.dev-cost {
  text-align: right;
}

/* ---------------- 说明 ---------------- */
.obs-list {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
  line-height: 1.6;
}

.empty {
  margin: 12px 0;
  text-align: center;
  color: var(--c-ink-muted);
  font-size: var(--fs-small);
}

@media (max-width: 1180px) {
  .kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
