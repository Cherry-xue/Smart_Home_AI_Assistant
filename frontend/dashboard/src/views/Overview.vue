<template>
  <div class="page">
    <!-- ---------------------------------------------------- KPI -->
    <div class="kpi-row">
      <StatCard label="在线设备" :value="`${stats.online ?? 0}`" :unit="`/ ${stats.total ?? 0}`" icon="Cpu" tone="ok" :hint="`房间 ${stats.rooms ?? 0} 间`" />
      <StatCard label="离线设备" :value="`${offlineCount}`" icon="WarnTriangleFilled" :tone="offlineCount ? 'warn' : 'normal'" hint="超过阈值未上报" />
      <StatCard label="高风险设备" :value="`${stats.high_risk ?? 0}`" icon="Lock" tone="danger" hint="门锁 / 燃气阀 / 总闸" />
      <StatCard label="近 24h 用电" :value="num(energy?.total_kwh, 2)" unit="kWh" icon="Lightning" :hint="windowText" />
      <StatCard label="近 24h 电费" :value="`¥${num(energy?.total_cost, 2)}`" icon="Money" :hint="按分时电价用代码算出" />
      <StatCard label="活跃告警" :value="`${alarmCount}`" icon="Bell" :tone="alarmCount ? 'danger' : 'ok'" hint="各房间 active 告警合计" />
    </div>

    <!-- ---------------------------------------------------- 房间卡片墙 -->
    <div class="panel">
      <div class="panel-title">
        房间态势
        <span class="sub">共 {{ rooms.length }} 间 · 数据更新 {{ lastRoomUpdate }}</span>
      </div>

      <div v-loading="loadingRooms" class="room-grid">
        <div v-for="r in rooms" :key="r.room_id" class="room-card" @click="gotoRoom(r)">
          <div class="rc-head">
            <span class="rc-name">{{ r.name }}</span>
            <span class="tag-dot" :class="r.online_count ? 'dot-ok' : 'dot-off'" />
            <span class="rc-online mono">{{ r.online_count }}/{{ r.device_count }}</span>
          </div>

          <div class="rc-metrics">
            <div v-for="m in (r.key_metrics || []).slice(0, 3)" :key="m.metric_code + m.device_id" class="rc-metric">
              <span class="rm-name">{{ m.device_name }}</span>
              <span class="rm-value mono">{{ metricText(m.value, m.unit) }}</span>
            </div>
            <div v-if="!(r.key_metrics || []).length" class="rc-metric dim">暂无读数</div>
          </div>

          <div class="rc-foot">
            <span v-if="r.high_risk_count" class="rc-badge danger">高风险 {{ r.high_risk_count }}</span>
            <span v-if="r.active_alarm_count" class="rc-badge danger">告警 {{ r.active_alarm_count }}</span>
            <span v-if="r.recent_event_count" class="rc-badge warn">事件 {{ r.recent_event_count }}</span>
            <span class="rc-time">{{ fmtTime(r.last_update_at) }}</span>
          </div>
        </div>

        <div v-if="!rooms.length && !loadingRooms" class="empty-box">没有房间数据</div>
      </div>
    </div>

    <!-- ---------------------------------------------------- 图表 -->
    <div class="chart-row">
      <div class="panel">
        <div class="panel-title">
          用电趋势
          <span class="sub">逐小时 kWh（{{ windowText }}）</span>
        </div>
        <EChart :option="hourlyOption" :height="250" />
      </div>

      <div class="panel">
        <div class="panel-title">
          用电排行
          <span class="sub">Top 8 设备</span>
        </div>
        <EChart :option="topDeviceOption" :height="250" />
      </div>
    </div>

    <!-- ---------------------------------------------------- 峰平谷 + 结论 -->
    <div class="chart-row">
      <div class="panel">
        <div class="panel-title">
          峰平谷构成
          <span class="sub">分时电价口径</span>
        </div>
        <EChart :option="periodOption" :height="240" />
      </div>

      <div class="panel">
        <div class="panel-title">
          系统观察到的事实
          <span class="sub">确定性代码算出，不是模型推测</span>
        </div>
        <ul v-if="observations.length" class="obs-list">
          <li v-for="(o, i) in observations" :key="i">{{ o }}</li>
        </ul>
        <div v-else class="empty-box">这个时间窗内没有可陈述的事实</div>
        <div v-if="energy?.note" class="obs-note">{{ energy.note }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSystemStore } from '@/stores/system'
import { getEnergySummary, getRooms } from '@/api'
import { fmtDateTime, fmtTime, fmtHour, metricText, num, periodLabel } from '@/utils/format'
import EChart from '@/components/EChart.vue'
import StatCard from '@/components/StatCard.vue'

const router = useRouter()
const system = useSystemStore()

const rooms = ref([])
const energy = ref(null)
const loadingRooms = ref(false)
const loadingEnergy = ref(false)
let timer = null

const stats = computed(() => system.deviceStats ?? {})
// ⚠️ health.devices 的键是 total / rooms / high_risk / online（没有 devices）
const offlineCount = computed(() => Math.max((stats.value.total ?? 0) - (stats.value.online ?? 0), 0))
const alarmCount = computed(() => rooms.value.reduce((s, r) => s + (r.active_alarm_count || 0), 0))
const observations = computed(() => energy.value?.observations ?? [])

const windowText = computed(() => {
  if (!energy.value) return '—'
  return `${fmtDateTime(energy.value.from)} → ${fmtDateTime(energy.value.to)}`
})

const lastRoomUpdate = computed(() => {
  const ts = rooms.value.map((r) => r.last_update_at).filter(Boolean).sort()
  return ts.length ? fmtDateTime(ts[ts.length - 1]) : '—'
})

async function loadRooms() {
  loadingRooms.value = true
  try {
    const d = await getRooms(1)
    rooms.value = d?.items ?? []
  } catch (e) {
    rooms.value = []
  } finally {
    loadingRooms.value = false
  }
}

async function loadEnergy() {
  loadingEnergy.value = true
  try {
    energy.value = await getEnergySummary()
  } catch (e) {
    energy.value = null
  } finally {
    loadingEnergy.value = false
  }
}

function gotoRoom(r) {
  router.push({ path: '/devices', query: { room_code: r.code } })
}

// ---------------------------------------------------------------- 图表
const AXIS_STYLE = {
  axisLine: { lineStyle: { color: '#2b3d5e' } },
  axisLabel: { color: '#8ba1c0', fontSize: 11 },
  splitLine: { lineStyle: { color: 'rgba(43,61,94,0.35)' } },
}

const hourlyOption = computed(() => {
  const hourly = energy.value?.hourly ?? []
  return {
    backgroundColor: 'transparent',
    grid: { left: 44, right: 16, top: 24, bottom: 30 },
    tooltip: { trigger: 'axis', backgroundColor: '#0e1526', borderColor: '#2b3d5e', textStyle: { color: '#d8e4f5' } },
    xAxis: {
      type: 'category',
      data: hourly.map((h) => fmtHour(h.hour)),
      ...AXIS_STYLE,
      splitLine: { show: false },
    },
    yAxis: { type: 'value', name: 'kWh', nameTextStyle: { color: '#5b739a' }, ...AXIS_STYLE },
    series: [
      {
        type: 'bar',
        data: hourly.map((h) => Number(h.kwh.toFixed(3))),
        itemStyle: {
          borderRadius: [3, 3, 0, 0],
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: '#2ea8ff' },
              { offset: 1, color: 'rgba(46,168,255,0.15)' },
            ],
          },
        },
        barMaxWidth: 18,
      },
    ],
  }
})

const topDeviceOption = computed(() => {
  const list = (energy.value?.by_device ?? []).slice(0, 8)
  return {
    backgroundColor: 'transparent',
    grid: { left: 90, right: 60, top: 12, bottom: 20 },
    tooltip: {
      trigger: 'item',
      backgroundColor: '#0e1526',
      borderColor: '#2b3d5e',
      textStyle: { color: '#d8e4f5' },
      formatter: (p) => `${p.name}<br/>用电 ${p.value} kWh`,
    },
    xAxis: { type: 'value', ...AXIS_STYLE },
    yAxis: {
      type: 'category',
      data: list.map((d) => d.name).reverse(),
      ...AXIS_STYLE,
      splitLine: { show: false },
    },
    series: [
      {
        type: 'bar',
        data: list.map((d) => Number(d.kwh.toFixed(3))).reverse(),
        itemStyle: { borderRadius: [0, 3, 3, 0], color: '#00e5c0' },
        barMaxWidth: 14,
        label: { show: true, position: 'right', color: '#8ba1c0', fontSize: 10 },
      },
    ],
  }
})

const periodOption = computed(() => {
  const data = (energy.value?.by_period ?? []).map((p) => ({
    name: periodLabel(p.period),
    value: Number(p.kwh.toFixed(3)),
  }))
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: '#0e1526',
      borderColor: '#2b3d5e',
      textStyle: { color: '#d8e4f5' },
      formatter: '{b}: {c} kWh ({d}%)',
    },
    legend: { bottom: 4, textStyle: { color: '#8ba1c0', fontSize: 11 } },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '45%'],
        data,
        itemStyle: { borderColor: '#0e1526', borderWidth: 2 },
        label: { color: '#c9d6e8', fontSize: 11, formatter: '{b}\n{d}%' },
      },
    ],
  }
})

onMounted(() => {
  loadRooms()
  loadEnergy()
  timer = setInterval(() => {
    loadRooms()
    loadEnergy()
  }, 15000)
})

onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 14px;
}

.room-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  min-height: 120px;
}

.room-card {
  padding: 12px 14px;
  border: 1px solid var(--border-soft);
  border-radius: 9px;
  background: #101a2e;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.15s;
}

.room-card:hover {
  border-color: var(--brand);
  transform: translateY(-2px);
}

.rc-head {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 10px;
}

.rc-name {
  font-size: 15px;
  font-weight: 600;
  color: #eaf2ff;
}

.rc-online {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-sub);
}

.rc-metric {
  display: flex;
  justify-content: space-between;
  padding: 3px 0;
  font-size: 12px;
  color: var(--text-sub);
}

.rc-metric.dim {
  color: var(--text-dim);
}

.rm-value {
  color: #d8e4f5;
}

.rc-foot {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #1e2b45;
}

.rc-badge {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
}

.rc-badge.danger {
  background: rgba(255, 92, 108, 0.14);
  color: #ff8b96;
}

.rc-badge.warn {
  background: rgba(255, 176, 32, 0.14);
  color: #ffc862;
}

.rc-time {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-dim);
}

.chart-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.obs-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.9;
  color: var(--text-sub);
}

.obs-note {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #1e2b45;
  font-size: 12px;
  color: var(--text-dim);
}

@media (max-width: 1400px) {
  .kpi-row {
    grid-template-columns: repeat(3, 1fr);
  }
}
</style>
