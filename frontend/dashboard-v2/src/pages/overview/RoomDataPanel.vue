<template>
  <aside class="panel">
    <header class="panel-head">
      <span class="panel-title">{{ room.name }}</span>
      <span class="panel-sub tnum">{{ room.device_count }} 台设备</span>
    </header>

    <!-- 概览数值 -->
    <dl class="stat-grid">
      <div class="stat">
        <dt>在线</dt>
        <dd class="tnum">{{ room.online_count ?? '—' }}</dd>
      </div>
      <div class="stat">
        <dt>离线</dt>
        <dd class="tnum" :class="{ 'is-warn': room.offline_count > 0 }">{{ room.offline_count ?? '—' }}</dd>
      </div>
      <div class="stat">
        <dt>高风险</dt>
        <dd class="tnum" :class="{ 'is-risk': room.high_risk_count > 0 }">{{ room.high_risk_count ?? '—' }}</dd>
      </div>
      <div class="stat">
        <dt>告警</dt>
        <dd class="tnum" :class="{ 'is-danger': room.active_alarm_count > 0 }">
          {{ room.active_alarm_count ?? '—' }}
        </dd>
      </div>
    </dl>

    <div class="meta-row">
      <span class="muted">数据更新</span>
      <span class="tnum">{{ fromNow(room.last_update_at, system.dataAnchor) }}</span>
    </div>

    <!-- 关键读数 -->
    <section class="section">
      <h3 class="section-title">关键读数</h3>
      <ul v-if="metrics.length" class="metric-list">
        <li v-for="(m, i) in metrics" :key="`${m.device_id}-${m.metric_code}-${i}`" class="metric">
          <div class="metric-top">
            <span class="metric-device">{{ m.device_name }}</span>
            <span class="tag" :class="qualityMeta(m.quality).cls">{{ qualityMeta(m.quality).label }}</span>
          </div>
          <div class="metric-value">
            <span class="metric-label">{{ metricLabel(m.metric_code) }}</span>
            <span class="metric-num tnum">{{ metricText(m.metric_code, m.value, m.unit) }}</span>
          </div>
        </li>
      </ul>
      <p v-else class="empty">这间房暂时没有可显示的读数。</p>
    </section>

    <p v-if="staleCount" class="panel-note">
      ⚠️ 有 {{ staleCount }} 条读数不是最新的（可能来自设备保留快照或补发）。
    </p>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import { useSystemStore } from '@/stores/system'
import { fromNow, metricLabel, metricText, qualityMeta } from '@/utils/format'

// 相对时间的基准要用**数据锚点**：模拟器时间领先真实时钟，用 Date.now() 会恒为"—"
const system = useSystemStore()

const props = defineProps({
  /** 房间卡片（/api/homes/1/rooms 的 items 之一） */
  room: { type: Object, required: true },
})

const metrics = computed(() => props.room?.key_metrics ?? [])

/** 观测时间明显落后于房间最后更新时间的条数 —— 提示"这不是当前值" */
const staleCount = computed(() => {
  const last = props.room?.last_update_at
  if (!last) return 0
  const lastT = new Date(String(last).replace(' ', 'T')).getTime()
  if (Number.isNaN(lastT)) return 0
  return metrics.value.filter((m) => {
    const t = new Date(String(m.observed_at || '').replace(' ', 'T')).getTime()
    return !Number.isNaN(t) && lastT - t > 5 * 60 * 1000
  }).length
})
</script>

<style scoped>
.panel {
  height: 100%;
  overflow-y: auto;
  padding: 14px 14px 18px;
  /* 设计稿里这块是**独立的框**（四周都有边），不是一条分隔线 */
  border: 1px solid var(--c-line);
}

.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--c-line);
}
.panel-title {
  font-size: 16px;
  font-weight: 500;
}
.panel-sub {
  font-size: var(--fs-small);
  color: var(--c-ink-muted);
}

.stat-grid {
  margin: 12px 0 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 10px;
}
.stat {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  border-bottom: 1px solid var(--c-line-soft);
  padding-bottom: 4px;
}
.stat dt {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.stat dd {
  margin: 0;
  font-size: 15px;
}
.is-warn {
  color: var(--c-warn);
}
.is-risk {
  color: var(--c-high-risk);
}
.is-danger {
  color: var(--c-danger);
}

.meta-row {
  margin-top: 10px;
  display: flex;
  justify-content: space-between;
  font-size: var(--fs-small);
}

.section {
  margin-top: 16px;
}
.section-title {
  margin: 0 0 8px;
  font-size: var(--fs-small);
  font-weight: 500;
  color: var(--c-ink-soft);
  letter-spacing: 0.5px;
}

.metric-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.metric {
  border-left: 2px solid var(--c-line);
  padding-left: 8px;
}
.metric-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.metric-device {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.metric-value {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
}
.metric-label {
  font-size: var(--fs-small);
  color: var(--c-ink-muted);
}
.metric-num {
  font-size: 16px;
}

.panel-note {
  margin: 14px 0 0;
  font-size: var(--fs-tiny);
  color: var(--c-warn);
  line-height: 1.6;
}
</style>
