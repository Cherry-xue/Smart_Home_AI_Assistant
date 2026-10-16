<template>
  <div class="page">
    <!-- ---------------------------------------------------- 时间窗 -->
    <div class="panel">
      <div class="filter-row">
        <span class="fr-label">时间范围</span>
        <el-radio-group v-model="preset" size="small" @change="onPreset">
          <el-radio-button :value="6">近 6 小时</el-radio-button>
          <el-radio-button :value="24">近 24 小时</el-radio-button>
          <el-radio-button :value="72">近 3 天</el-radio-button>
          <el-radio-button :value="168">近 7 天</el-radio-button>
        </el-radio-group>

        <el-date-picker
          v-model="customRange"
          type="datetimerange"
          size="small"
          value-format="YYYY-MM-DDTHH:mm:ss"
          start-placeholder="开始"
          end-placeholder="结束"
          style="width: 360px"
          @change="onCustom"
        />

        <el-button size="small" :loading="loading" @click="load">刷新</el-button>

        <span class="fr-note">
          金额由后端按分时电价<b>用代码算出</b>，不是模型估算（规划书硬要求）
        </span>
      </div>
    </div>

    <!-- ---------------------------------------------------- KPI -->
    <div class="kpi-row">
      <StatCard label="总用电" :value="num(data?.total_kwh, 3)" unit="kWh" icon="Lightning" :hint="rangeText" />
      <StatCard label="总电费" :value="`¥${num(data?.total_cost, 2)}`" icon="Money" hint="峰平谷分时计价" />
      <StatCard label="平均单价" :value="`¥${num(avgPrice, 3)}`" unit="/kWh" icon="PriceTag" hint="总费用 ÷ 总电量" />
      <StatCard label="峰值功率" :value="num(peakPower, 0)" unit="W" icon="Odometer" :tone="peakPower > 3000 ? 'warn' : 'normal'" hint="所有设备中的最大值" />
    </div>

    <!-- ---------------------------------------------------- 峰平谷 -->
    <div class="two-col">
      <div class="panel">
        <div class="panel-title">
          峰平谷构成
          <span class="sub">分时电价 · 谷段便宜就该往谷段挪</span>
        </div>

        <el-table v-loading="loading" :data="data?.by_period ?? []" size="small">
          <el-table-column label="时段" width="90">
            <template #default="{ row }">
              <span class="period-dot" :style="{ background: periodColor(row.period) }" />
              {{ periodLabel(row.period) }}
            </template>
          </el-table-column>
          <el-table-column label="单价" width="100" align="right">
            <template #default="{ row }">
              <span class="mono">{{ row.price === null ? '—' : `¥${row.price.toFixed(2)}` }}</span>
            </template>
          </el-table-column>
          <el-table-column label="电量" width="100" align="right">
            <template #default="{ row }"><span class="mono">{{ num(row.kwh, 3) }} kWh</span></template>
          </el-table-column>
          <el-table-column label="电费" width="100" align="right">
            <template #default="{ row }"><span class="mono">¥{{ num(row.cost, 2) }}</span></template>
          </el-table-column>
          <el-table-column label="占比" min-width="150">
            <template #default="{ row }">
              <el-progress :percentage="Math.round((row.share ?? 0) * 100)" :stroke-width="10" :color="periodColor(row.period)" />
            </template>
          </el-table-column>
          <el-table-column label="平均功率" width="100" align="right">
            <template #default="{ row }"><span class="mono">{{ num(row.avg_power_w, 0) }} W</span></template>
          </el-table-column>
        </el-table>

        <div class="tariff-note">
          当前电价方案：<b>{{ tariffName }}</b>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          用电结构
          <span class="sub">各时段电量占比</span>
        </div>
        <EChart :option="periodPieOption" :height="240" />
      </div>
    </div>

    <!-- ---------------------------------------------------- 逐小时 -->
    <div class="panel">
      <div class="panel-title">
        逐小时用电与功率
        <span class="sub">柱=电量 kWh · 线=平均功率 W</span>
      </div>
      <EChart :option="hourlyOption" :height="270" />
    </div>

    <!-- ---------------------------------------------------- 设备排行 -->
    <div class="two-col">
      <div class="panel">
        <div class="panel-title">
          设备用电排行
          <span class="sub">按电量降序</span>
        </div>
        <el-table v-loading="loading" :data="data?.by_device ?? []" size="small" max-height="320">
          <el-table-column prop="name" label="设备" min-width="130" />
          <el-table-column prop="room_name" label="房间" width="90" />
          <el-table-column label="电量" width="100" align="right">
            <template #default="{ row }"><span class="mono">{{ num(row.kwh, 3) }}</span></template>
          </el-table-column>
          <el-table-column label="电费" width="90" align="right">
            <template #default="{ row }"><span class="mono">¥{{ num(row.cost, 2) }}</span></template>
          </el-table-column>
          <el-table-column label="占比" width="140">
            <template #default="{ row }">
              <el-progress :percentage="Math.round((row.share ?? 0) * 100)" :stroke-width="10" color="#00e5c0" />
            </template>
          </el-table-column>
          <el-table-column label="峰值功率" width="100" align="right">
            <template #default="{ row }"><span class="mono">{{ num(row.peak_power_w, 0) }} W</span></template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-title">
          Top 8 用电设备
          <span class="sub">横向对比</span>
        </div>
        <EChart :option="deviceBarOption" :height="320" />

        <div class="sec-title">系统观察到的事实</div>
        <ul v-if="(data?.observations ?? []).length" class="obs-list">
          <li v-for="(o, i) in data.observations" :key="i">{{ o }}</li>
        </ul>
        <div v-else class="empty-box">这个时间窗内没有可陈述的事实</div>
        <div v-if="data?.note" class="obs-note">{{ data.note }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { getEnergySummary } from '@/api'
import { useSystemStore } from '@/stores/system'
import { fmtDateTime, fmtHour, num, periodLabel, PERIOD_META } from '@/utils/format'
import EChart from '@/components/EChart.vue'
import StatCard from '@/components/StatCard.vue'

const system = useSystemStore()

const data = ref(null)
const loading = ref(false)
const preset = ref(24)
const customRange = ref(null)

const tariffName = computed(() => system.health?.tariff ?? '—')

const rangeText = computed(() => {
  if (!data.value) return '—'
  return `${fmtDateTime(data.value.from)} → ${fmtDateTime(data.value.to)}`
})

const avgPrice = computed(() => {
  const d = data.value
  if (!d || !d.total_kwh) return null
  return d.total_cost / d.total_kwh
})

const peakPower = computed(() => {
  const list = data.value?.by_device ?? []
  return list.length ? Math.max(...list.map((d) => d.peak_power_w || 0)) : null
})

function periodColor(p) {
  return PERIOD_META[p]?.color ?? '#5b739a'
}

const iso = (d) => {
  // 后端要的是 naive 本地时间字符串，不能带 Z / 时区
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

async function load() {
  loading.value = true
  try {
    const params = {}
    // 缺省窗口以数据锚点为基准；自定义时按用户给的时间
    if (customRange.value?.length === 2) {
      params.from = customRange.value[0]
      params.to = customRange.value[1]
    } else {
      const anchor = system.dataAnchor ? new Date(system.dataAnchor) : new Date()
      params.from = iso(new Date(anchor.getTime() - preset.value * 3600 * 1000))
      params.to = iso(anchor)
    }
    data.value = await getEnergySummary(params)
  } catch (e) {
    data.value = null
  } finally {
    loading.value = false
  }
}

function onPreset() {
  customRange.value = null
  load()
}

function onCustom(v) {
  if (!v) {
    customRange.value = null
    load()
  } else {
    load()
  }
}

// ---------------------------------------------------------------- 图表
const AXIS = {
  axisLine: { lineStyle: { color: '#2b3d5e' } },
  axisLabel: { color: '#8ba1c0', fontSize: 11 },
  splitLine: { lineStyle: { color: 'rgba(43,61,94,0.35)' } },
}

const hourlyOption = computed(() => {
  const hourly = data.value?.hourly ?? []
  return {
    backgroundColor: 'transparent',
    grid: { left: 50, right: 55, top: 30, bottom: 30 },
    legend: { top: 0, textStyle: { color: '#8ba1c0', fontSize: 11 } },
    tooltip: { trigger: 'axis', backgroundColor: '#0e1526', borderColor: '#2b3d5e', textStyle: { color: '#d8e4f5' } },
    xAxis: { type: 'category', data: hourly.map((h) => fmtHour(h.hour)), ...AXIS, splitLine: { show: false } },
    yAxis: [
      { type: 'value', name: 'kWh', nameTextStyle: { color: '#5b739a' }, ...AXIS },
      { type: 'value', name: 'W', nameTextStyle: { color: '#5b739a' }, ...AXIS, splitLine: { show: false } },
    ],
    series: [
      {
        name: '电量',
        type: 'bar',
        data: hourly.map((h) => Number(h.kwh.toFixed(3))),
        itemStyle: { color: '#2ea8ff', borderRadius: [3, 3, 0, 0] },
        barMaxWidth: 16,
      },
      {
        name: '平均功率',
        type: 'line',
        yAxisIndex: 1,
        smooth: true,
        symbol: 'none',
        data: hourly.map((h) => Math.round(h.avg_power_w ?? 0)),
        lineStyle: { color: '#ffb020', width: 2 },
        itemStyle: { color: '#ffb020' },
      },
    ],
  }
})

const periodPieOption = computed(() => {
  const list = data.value?.by_period ?? []
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: '#0e1526',
      borderColor: '#2b3d5e',
      textStyle: { color: '#d8e4f5' },
      formatter: (p) => `${p.name}<br/>${p.value} kWh · ¥${(list.find((x) => periodLabel(x.period) === p.name)?.cost ?? 0).toFixed(2)}`,
    },
    legend: { bottom: 0, textStyle: { color: '#8ba1c0', fontSize: 11 } },
    series: [
      {
        type: 'pie',
        radius: ['40%', '66%'],
        center: ['50%', '45%'],
        data: list.map((p) => ({ name: periodLabel(p.period), value: Number(p.kwh.toFixed(3)), itemStyle: { color: periodColor(p.period) } })),
        itemStyle: { borderColor: '#0e1526', borderWidth: 2 },
        label: { color: '#c9d6e8', fontSize: 11, formatter: '{b}\n{d}%' },
      },
    ],
  }
})

const deviceBarOption = computed(() => {
  const list = (data.value?.by_device ?? []).slice(0, 8)
  return {
    backgroundColor: 'transparent',
    grid: { left: 95, right: 55, top: 12, bottom: 20 },
    tooltip: { trigger: 'item', backgroundColor: '#0e1526', borderColor: '#2b3d5e', textStyle: { color: '#d8e4f5' }, formatter: '{b}: {c} kWh' },
    xAxis: { type: 'value', ...AXIS },
    yAxis: { type: 'category', data: list.map((d) => d.name).reverse(), ...AXIS, splitLine: { show: false } },
    series: [
      {
        type: 'bar',
        data: list.map((d) => Number(d.kwh.toFixed(3))).reverse(),
        itemStyle: { color: '#00e5c0', borderRadius: [0, 3, 3, 0] },
        barMaxWidth: 14,
        label: { show: true, position: 'right', color: '#8ba1c0', fontSize: 10 },
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

.fr-label {
  font-size: 13px;
  color: var(--text-sub);
}

.fr-note {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-dim);
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
}

.two-col {
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  gap: 16px;
}

.period-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 6px;
  border-radius: 50%;
  vertical-align: middle;
}

.tariff-note {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #1e2b45;
  font-size: 12px;
  color: var(--text-dim);
}

.sec-title {
  margin: 14px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: #eaf2ff;
}

.obs-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.9;
  color: var(--text-sub);
}

.obs-note {
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-dim);
}

@media (max-width: 1400px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}
</style>
