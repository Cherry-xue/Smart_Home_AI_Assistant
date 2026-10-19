<template>
  <!-- `page-fade`：切页淡入淡出。本页无下方菜单，所以不标 `page-slide` -->
  <div class="page page-fade">
    <!-- ==================== 顶部 ==================== -->
    <div class="panel top-strip">
      <div class="ts-left">
        <span class="ts-title">照护与安防</span>
        <span class="muted tiny">数据时间 {{ fmtDateTime(anchor) }}</span>
      </div>
      <div class="ts-right">
        <el-button size="small" :loading="loading" @click="loadAll">刷新</el-button>
      </div>
    </div>

    <!-- 覆盖率不足时**先说数据问题，再说结论**，别让人拿不足的数据当事实 -->
    <!-- ⚠️ 必须写 `.length`：空数组在 JS 里是真值，直接 `v-if="insufficient"` 会永远显示 -->
    <div v-if="insufficient.length" class="panel warn-strip">
      <b>⚠️ 感知数据覆盖不足</b>
      <span>{{ insufficientText }}</span>
    </div>

    <div class="grid">
      <!-- ==================== 左：安防告警 ==================== -->
      <section class="panel col-alarms">
        <header class="sec-head">
          <h3 class="sec-title">安防告警</h3>
          <div class="chips">
            <span class="chip chip--critical">严重 {{ alarmByLevel.critical || 0 }}</span>
            <span class="chip chip--warning">警告 {{ alarmByLevel.warning || 0 }}</span>
            <span class="chip">近 24 小时</span>
          </div>
        </header>

        <div class="alarm-list">
          <article v-for="a in alarms" :key="a.id" class="alarm" :class="`alarm--${a.level}`">
            <div class="alarm-head">
              <span class="lvl" :class="`lvl--${a.level}`">{{ levelLabel(a.level) }}</span>
              <span class="alarm-title">{{ a.title }}</span>
              <span class="alarm-time tnum">{{ fmtDateTime(a.created_at) }}</span>
            </div>

            <div class="alarm-meta">
              <span v-if="a.device_name" class="muted">{{ a.device_name }}</span>
              <span v-if="a.room_name" class="muted">· {{ a.room_name }}</span>
              <span class="status" :class="`status--${a.status}`">{{ statusLabel(a.status) }}</span>
            </div>

            <!-- detail 是纯文本多行：判据 / 来源设备 / 证据 / 建议处置 / 阈值说明 -->
            <pre class="alarm-detail">{{ a.detail }}</pre>

            <div class="alarm-actions">
              <el-button
                v-if="a.status === 'active'"
                size="small"
                :loading="busy === `ack-${a.id}`"
                @click="ack(a)"
              >
                已知悉
              </el-button>
            </div>
          </article>

          <p v-if="!alarms.length" class="empty">近 24 小时没有告警</p>
        </div>
      </section>

      <!-- ==================== 右：照护 + 建议 ==================== -->
      <div class="col-right">
        <section class="panel">
          <header class="sec-head">
            <h3 class="sec-title">照护节律</h3>
            <span class="muted tiny">{{ coverageText }}</span>
          </header>

          <ul class="timeline">
            <li
              v-for="o in observations"
              :key="o.id"
              class="tl-item"
              :class="{ 'tl-item--clue': o.is_clue }"
            >
              <div class="tl-head">
                <span class="tl-time tnum">{{ fmtDateTime(o.observed_at) }}</span>
                <span class="tl-type" :class="`tl-type--${o.activity_type}`">{{ o.activity_label }}</span>
                <span v-if="o.duration_min" class="tl-dur tnum">{{ o.duration_min }} 分钟</span>
                <span v-if="o.confidence !== null" class="tl-conf tnum">
                  可信度 {{ Math.round(o.confidence * 100) }}%
                </span>
              </div>
              <p class="tl-remark tiny muted">{{ o.remark }}</p>
            </li>
          </ul>
          <p v-if="!observations.length" class="empty">暂无照护观察</p>
        </section>

        <section class="panel">
          <header class="sec-head">
            <h3 class="sec-title">主动建议</h3>
          </header>

          <div class="sug-list">
            <article
              v-for="s in suggestions"
              :key="s.id"
              class="sug"
              :class="`sug--${s.disturbance_level}`"
            >
              <div class="sug-head">
                <span class="sug-lvl" :class="`sug-lvl--${s.disturbance_level}`">
                  {{ disturbanceLabel(s.disturbance_level) }}
                </span>
                <span class="sug-content">{{ s.content }}</span>
              </div>

              <p class="sug-reason tiny muted">
                <span class="reason-key">{{ reasonText(s) }}</span>
                <span v-if="s.quiet_downgraded" class="quiet-tag">免打扰已降级</span>
              </p>

              <div v-if="s.is_open" class="sug-actions">
                <el-button size="small" text :loading="busy === `dismiss-${s.id}`" @click="dismiss(s)">
                  忽略
                </el-button>
                <el-button
                  size="small"
                  text
                  type="primary"
                  :loading="busy === `accept-${s.id}`"
                  @click="accept(s)"
                >
                  采纳
                </el-button>
              </div>
              <div v-else class="sug-actions">
                <span class="tiny muted">{{ statusLabel(s.status) }}</span>
              </div>
            </article>

            <p v-if="!suggestions.length" class="empty">暂时没有建议</p>
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  acceptSuggestion,
  ackAlarm,
  dismissSuggestion,
  getHealth,
  listAlarms,
  listCareObservations,
  listSuggestions,
} from '@/api'
import { useHomeStore } from '@/stores/home'
import { fmtDateTime } from '@/utils/format'

const home = useHomeStore()
const loading = ref(false)
const busy = ref('')
const anchor = ref(null)

const alarms = ref([])
const alarmByLevel = ref({})
const syncInfo = ref(null)

const observations = ref([])
const coverage = ref([])

const suggestions = ref([])

const LEVEL_LABEL = { info: '提示', warning: '警告', critical: '严重' }
const STATUS_LABEL = {
  active: '待处理',
  acknowledged: '已知悉',
  resolved: '已恢复',
  dismissed: '已忽略',
  pending: '待处理',
  shown: '已展示',
  accepted: '已采纳',
}
const DISTURBANCE_LABEL = { urgent: '紧急', important: '重要', normal: '常规', silent: '静默' }
const RULE_LABEL = {
  alarm_followup: '来源：一条活跃告警',
  peak_share_high: '来源：峰时段用电占比偏高',
  always_on_light: '来源：灯亮着但房间无人',
}

const levelLabel = (v) => LEVEL_LABEL[v] ?? v
const statusLabel = (v) => STATUS_LABEL[v] ?? v
const disturbanceLabel = (v) => DISTURBANCE_LABEL[v] ?? v

/** 覆盖率不足的对象（sufficient=false） */
const insufficient = computed(() => coverage.value.filter((c) => !c.sufficient))
const insufficientText = computed(() =>
  insufficient.value
    .map(
      (c) =>
        `${c.nickname}：覆盖 ${c.buckets_actual}/${c.buckets_expected} 桶（${Math.round(
          c.coverage_ratio * 100,
        )}%），未做节律判断`,
    )
    .join(' '),
)

const coverageText = computed(() => {
  if (!coverage.value.length) return ''
  return coverage.value
    .map((c) => `${c.nickname}：采样覆盖 ${c.buckets_actual}/${c.buckets_expected} 桶`)
    .join(' · ')
})

/** 建议的理由：优先展示判据，没有判据就展示规则名 */
function reasonText(s) {
  const r = s.reason || {}
  if (r.criteria) return r.criteria
  if (r.rule === 'alarm_followup') return `由告警 #${r.alarm_id}「${r.alarm_level}」派生`
  return RULE_LABEL[r.rule] || r.rule || ''
}

async function loadAll() {
  loading.value = true
  try {
    const hid = home.currentId
    const [health, alarm, care, sug] = await Promise.all([
      getHealth(),
      listAlarms({ home_id: hid }),
      listCareObservations({ home_id: hid }),
      listSuggestions({ home_id: hid }),
    ])
    anchor.value = health?.data_anchor ?? null

    alarms.value = alarm?.items ?? []
    alarmByLevel.value = alarm?.by_level ?? {}
    syncInfo.value = alarm?.sync ?? null

    observations.value = care?.items ?? []
    coverage.value = care?.coverage ?? []

    suggestions.value = sug?.items ?? []
  } catch (e) {
    ElMessage({ message: `加载失败：${e.message}`, type: 'error', duration: 4500 })
  } finally {
    loading.value = false
  }
}

async function ack(a) {
  busy.value = `ack-${a.id}`
  try {
    const out = await ackAlarm(a.id)
    ElMessage({ message: out.message, type: 'success', duration: 5000 })
    await loadAll()
  } catch (e) {
    ElMessage({ message: `确认失败：${e.message}`, type: 'error', duration: 4500 })
  } finally {
    busy.value = ''
  }
}

async function dismiss(s) {
  busy.value = `dismiss-${s.id}`
  try {
    const out = await dismissSuggestion(s.id)
    ElMessage({ message: out.message, type: 'success', duration: 4200 })
    await loadAll()
  } catch (e) {
    ElMessage({ message: `忽略失败：${e.message}`, type: 'error', duration: 4500 })
  } finally {
    busy.value = ''
  }
}

async function accept(s) {
  busy.value = `accept-${s.id}`
  try {
    const out = await acceptSuggestion(s.id)
    ElMessage({ message: out.message, type: 'success', duration: 5200 })
    await loadAll()
  } catch (e) {
    ElMessage({ message: `采纳失败：${e.message}`, type: 'error', duration: 4500 })
  } finally {
    busy.value = ''
  }
}

watch(() => home.currentId, loadAll)

let timer = null
onMounted(async () => {
  await loadAll()
  // 20 秒轮询：告警推导是幂等的，重复拉不会堆数据
  timer = setInterval(loadAll, 20000)
})
onUnmounted(() => clearInterval(timer))
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

/* ---------------- 顶部 ---------------- */
.top-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--gap);
  flex: 0 0 auto;
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
  gap: 12px;
}
.ts-note {
  max-width: 46ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.warn-strip {
  display: flex;
  gap: 10px;
  align-items: baseline;
  border-color: var(--c-warn);
  background: #fdf6e9;
  font-size: var(--fs-small);
  line-height: 1.6;
}

/* ---------------- 布局 ---------------- */
.grid {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  gap: var(--gap);
  flex: 1;
  min-height: 0;
}
.col-alarms {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.col-right {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  min-height: 0;
  overflow: auto;
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
.chips {
  display: flex;
  gap: 6px;
}
.chip {
  font-size: var(--fs-tiny);
  padding: 1px 7px;
  border: 1px solid var(--c-line);
  border-radius: 999px;
  color: var(--c-ink-soft);
}
.chip--critical {
  border-color: var(--c-danger);
  color: var(--c-danger);
}
.chip--warning {
  border-color: var(--c-warn);
  color: var(--c-warn);
}

/* ---------------- 告警 ---------------- */
.alarm-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow: auto;
  min-height: 0;
  padding-right: 2px;
}
.alarm {
  border: 1px solid var(--c-line);
  border-left: 3px solid var(--c-line);
  border-radius: var(--radius);
  padding: 10px 12px;
  background: var(--c-surface-soft);
}
.alarm--critical {
  border-left-color: var(--c-danger);
}
.alarm--warning {
  border-left-color: var(--c-warn);
}
.alarm--info {
  border-left-color: var(--c-accent);
}

.alarm-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.alarm-title {
  font-weight: 600;
  font-size: var(--fs-small);
  flex: 1;
  min-width: 0;
}
.alarm-time {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.lvl {
  font-size: var(--fs-tiny);
  padding: 0 6px;
  border-radius: 3px;
  border: 1px solid currentColor;
}
.lvl--critical {
  color: var(--c-danger);
}
.lvl--warning {
  color: var(--c-warn);
}
.lvl--info {
  color: var(--c-accent);
}

.alarm-meta {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-top: 4px;
  font-size: var(--fs-tiny);
}
.status {
  margin-left: auto;
  padding: 0 6px;
  border-radius: 3px;
  border: 1px solid var(--c-line);
  color: var(--c-ink-soft);
}
.status--active {
  border-color: var(--c-danger);
  color: var(--c-danger);
}
.status--acknowledged {
  border-color: var(--c-accent);
  color: var(--c-accent);
}
.status--resolved {
  color: var(--c-good);
  border-color: var(--c-good);
}

/* detail 是纯文本多行，必须保留换行 */
.alarm-detail {
  margin: 8px 0 0;
  padding: 8px 10px;
  background: var(--c-surface);
  border: 1px dashed var(--c-line-soft);
  border-radius: 3px;
  font-family: inherit;
  font-size: var(--fs-tiny);
  line-height: 1.7;
  color: var(--c-ink-soft);
  white-space: pre-wrap;
  word-break: break-word;
}

.alarm-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
}

/* ---------------- 照护时间轴 ---------------- */
.timeline {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tl-item {
  border-left: 2px solid var(--c-line);
  padding-left: 10px;
}
.tl-item--clue {
  border-left-color: var(--c-warn);
}
.tl-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.tl-time {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.tl-type {
  font-size: var(--fs-small);
  font-weight: 600;
  padding: 0 6px;
  border-radius: 3px;
  background: var(--c-accent-soft);
  color: var(--c-accent);
}
.tl-type--in_bed {
  background: #eef2f8;
  color: #5a6b86;
}
.tl-type--no_activity {
  background: #fdf0e6;
  color: var(--c-warn);
}
.tl-type--insufficient_data {
  background: #f4f4f4;
  color: var(--c-ink-muted);
}
.tl-dur,
.tl-conf {
  font-size: var(--fs-tiny);
  color: var(--c-ink-soft);
}
.tl-remark {
  margin: 3px 0 0;
  line-height: 1.6;
}

/* ---------------- 建议 ---------------- */
.sug-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sug {
  border: 1px solid var(--c-line);
  border-radius: var(--radius);
  padding: 9px 11px;
  background: var(--c-surface-soft);
}
.sug--urgent {
  border-color: #f0c6c0;
  background: #fdf4f2;
}
.sug-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.sug-lvl {
  font-size: var(--fs-tiny);
  padding: 0 6px;
  border-radius: 3px;
  border: 1px solid currentColor;
  color: var(--c-ink-soft);
  flex: 0 0 auto;
}
.sug-lvl--urgent {
  color: var(--c-danger);
}
.sug-lvl--important {
  color: var(--c-warn);
}
.sug-lvl--silent {
  color: var(--c-ink-muted);
}
.sug-content {
  font-size: var(--fs-small);
  line-height: 1.6;
}
.sug-reason {
  margin: 6px 0 0;
  line-height: 1.6;
}
.reason-key {
  word-break: break-word;
}
.quiet-tag {
  margin-left: 6px;
  padding: 0 5px;
  border: 1px solid var(--c-line);
  border-radius: 3px;
}
.sug-actions {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
  margin-top: 4px;
}

.empty {
  margin: 12px 0;
  text-align: center;
  color: var(--c-ink-muted);
  font-size: var(--fs-small);
}

@media (max-width: 1180px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
