<template>
  <div class="page page-fade">
    <!-- ==================== 顶部 ==================== -->
    <div class="panel top-strip">
      <div class="ts-left">
        <span class="ts-title">系统状态</span>
        <span class="muted tiny">数据时间 {{ fmtDateTime(anchor) }}</span>
      </div>
      <div class="ts-right">
        <el-button size="small" :loading="system.loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <p v-if="system.healthError" class="panel err">
      无法连接后端：{{ system.healthError }}
      <span class="err-tip">确认 backend/run_api.py 已在运行，且 Vite 代理目标（默认 127.0.0.1:8000）正确。</span>
    </p>

    <p v-else-if="!system.health" class="empty">正在读取系统状态…</p>

    <template v-if="system.health">
      <!-- ==================== KPI ==================== -->
      <div class="kpi-row">
        <div class="kpi">
          <span class="kpi-label">服务状态</span>
          <span class="kpi-value" :class="health.status === 'ok' ? 'ok' : 'warn'">
            {{ health.status === 'ok' ? '正常' : '降级' }}
          </span>
          <span class="kpi-hint muted">{{ health.app_env }}</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">数据来源</span>
          <span class="kpi-value">{{ dataModeLabel(health.data_mode) }}</span>
          <span class="kpi-hint muted">{{ anchor ? `数据时间 ${fmtDateTime(anchor)}` : '暂无数据' }}</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">设备</span>
          <span class="kpi-value tnum">{{ dev.total ?? 0 }}</span>
          <span class="kpi-hint muted">在线 <span class="tnum">{{ dev.online ?? 0 }}</span> 台</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">收到报文</span>
          <span class="kpi-value tnum">{{ ingest.received ?? 0 }}</span>
          <span class="kpi-hint muted">入库 <span class="tnum">{{ ingest.readings ?? 0 }}</span> 条读数</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">丢弃 / 异常</span>
          <span class="kpi-value tnum" :class="ingest.rejected ? 'warn' : ''">
            {{ (ingest.rejected ?? 0) + (ingest.unknown_device ?? 0) }}
          </span>
          <span class="kpi-hint muted">拒收 + 未知设备</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">高风险自动执行</span>
          <span class="kpi-value" :class="health.high_risk_auto_exec ? 'danger' : 'ok'">
            {{ health.high_risk_auto_exec ? '允许' : '禁止' }}
          </span>
          <span class="kpi-hint muted">红线：恒为禁止</span>
        </div>
      </div>

      <div class="two-col">
        <!-- ==================== 数据库 ==================== -->
        <section class="panel">
          <header class="sec-head">
            <h3 class="sec-title">数据库</h3>
            <span class="muted tiny">{{ db.current_user }}@远程 MySQL</span>
          </header>
          <dl class="kv">
            <div class="kv-row">
              <dt>连通</dt>
              <dd><span class="tag" :class="db.ok ? 'tag--good' : 'tag--danger'">{{ db.ok ? '正常' : '异常' }}</span></dd>
            </div>
            <div class="kv-row"><dt>版本</dt><dd class="mono">{{ db.version ?? '—' }}</dd></div>
            <div class="kv-row"><dt>字符集</dt><dd class="mono">{{ db.charset ?? '—' }}</dd></div>
            <div class="kv-row">
              <dt>响应耗时</dt>
              <dd class="tnum">{{ db.response_ms === null ? '—' : `${db.response_ms} ms` }}</dd>
            </div>
            <div v-if="db.error" class="kv-row">
              <dt>错误</dt>
              <dd class="err-text">{{ db.error }}</dd>
            </div>
          </dl>
        </section>

        <!-- ==================== MQTT ==================== -->
        <section class="panel">
          <header class="sec-head">
            <h3 class="sec-title">MQTT 接入</h3>
            <span class="muted tiny">{{ mqtt.enabled ? '已启用' : '未启用' }}</span>
          </header>
          <dl class="kv">
            <div class="kv-row">
              <dt>连接</dt>
              <dd>
                <span class="tag" :class="mqtt.connected ? 'tag--good' : 'tag--danger'">
                  {{ mqtt.connected ? '已连接' : '未连接' }}
                </span>
              </dd>
            </div>
            <div class="kv-row">
              <dt>订阅线程</dt>
              <dd>{{ mqtt.thread_alive ? '存活' : '已退出' }}</dd>
            </div>
            <div class="kv-row">
              <dt>运行时长</dt>
              <dd class="tnum">{{ mqtt.uptime_sec ? `${Math.round(mqtt.uptime_sec)} s` : '—' }}</dd>
            </div>
            <div class="kv-row">
              <dt>会话 / 重连</dt>
              <dd class="tnum">{{ mqtt.sessions ?? 0 }} / {{ mqtt.reconnects ?? 0 }}</dd>
            </div>
            <div class="kv-row">
              <dt>已收报文</dt>
              <dd class="tnum">{{ mqtt.messages_seen ?? 0 }}</dd>
            </div>
            <div class="kv-row">
              <dt>最后一条</dt>
              <dd>{{ fmtDateTime(mqtt.last_message_at) }}</dd>
            </div>
            <div v-if="mqtt.error" class="kv-row">
              <dt>错误</dt>
              <dd class="err-text">{{ mqtt.error }}</dd>
            </div>
          </dl>
        </section>
      </div>

      <!-- ==================== 落库统计 ==================== -->
      <section class="panel">
        <header class="sec-head">
          <h3 class="sec-title">数据落库统计</h3>
          <span class="muted tiny">坏数据入库但打标，不静默丢弃</span>
        </header>

        <div class="ingest-grid">
          <div class="ig-cell"><span class="ig-num tnum">{{ ingest.received ?? 0 }}</span><span class="ig-label">收到</span></div>
          <div class="ig-cell"><span class="ig-num tnum">{{ ingest.readings ?? 0 }}</span><span class="ig-label">读数行</span></div>
          <div class="ig-cell"><span class="ig-num tnum">{{ ingest.energies ?? 0 }}</span><span class="ig-label">能耗行</span></div>
          <div class="ig-cell"><span class="ig-num tnum">{{ ingest.events ?? 0 }}</span><span class="ig-label">事件</span></div>
          <div class="ig-cell ig-cell--warn"><span class="ig-num tnum">{{ ingest.rejected ?? 0 }}</span><span class="ig-label">拒收</span></div>
          <div class="ig-cell ig-cell--warn"><span class="ig-num tnum">{{ ingest.unknown_device ?? 0 }}</span><span class="ig-label">未知设备</span></div>
          <div class="ig-cell ig-cell--warn"><span class="ig-num tnum">{{ ingest.duplicates ?? 0 }}</span><span class="ig-label">重复 seq</span></div>
          <div class="ig-cell ig-cell--warn"><span class="ig-num tnum">{{ ingest.stale ?? 0 }}</span><span class="ig-label">陈旧数据</span></div>
          <div class="ig-cell ig-cell--warn"><span class="ig-num tnum">{{ ingest.out_of_range ?? 0 }}</span><span class="ig-label">越界</span></div>
          <div class="ig-cell"><span class="ig-num tnum">{{ ingest.retained_snapshots ?? 0 }}</span><span class="ig-label">retained 快照</span></div>
          <div class="ig-cell ig-cell--danger"><span class="ig-num tnum">{{ ingest.errors ?? 0 }}</span><span class="ig-label">入库错误</span></div>
        </div>

        <p v-if="ingest.last_error" class="err-text">{{ ingest.last_error }}</p>
      </section>

      <div class="two-col">
        <!-- ==================== 缓存 ==================== -->
        <section class="panel">
          <header class="sec-head">
            <h3 class="sec-title">最新值缓存</h3>
            <span class="muted tiny">只收 good 且非陈旧的数据</span>
          </header>
          <dl class="kv">
            <div class="kv-row"><dt>缓存设备</dt><dd class="tnum">{{ cache.devices ?? 0 }} 台</dd></div>
            <div class="kv-row"><dt>缓存值</dt><dd class="tnum">{{ cache.values ?? 0 }} 个</dd></div>
            <div class="kv-row"><dt>启动预热</dt><dd class="tnum">{{ cache.preloaded ?? 0 }} 条</dd></div>
            <div class="kv-row"><dt>离线设备</dt><dd class="tnum">{{ cache.offline_count ?? 0 }} 台</dd></div>
          </dl>
          <!-- ⚠️ 必须写 .length：空数组在 JS 里是真值 -->
          <div v-if="(cache.offline_devices || []).length" class="offline-list">
            <span v-for="c in cache.offline_devices" :key="c" class="tag mono">{{ c }}</span>
          </div>
        </section>

        <!-- ==================== 电价 ==================== -->
        <section class="panel">
          <header class="sec-head">
            <h3 class="sec-title">电价方案</h3>
            <span class="muted tiny">分时电价，金额由代码计算</span>
          </header>
          <div class="tariff-box">{{ health.tariff }}</div>
          <p class="side-note muted">
            数据时间锚点 <b class="mono">{{ fmtDateTime(health.data_anchor) }}</b>
            —— 缺省时间窗以它为基准，所以"近 24 小时"指的是数据里的最近 24 小时。
          </p>
        </section>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useSystemStore } from '@/stores/system'
import { dataModeLabel, fmtDateTime } from '@/utils/format'

const system = useSystemStore()

/** ⚠️ /api/health 的 devices 键是 total / rooms / high_risk / online */
const health = computed(() => system.health ?? {})
const dev = computed(() => health.value.devices ?? {})
const db = computed(() => health.value.database ?? {})
const mqtt = computed(() => health.value.mqtt ?? {})
const ingest = computed(() => health.value.ingest ?? {})
const cache = computed(() => health.value.cache ?? {})
const anchor = computed(() => health.value.data_anchor ?? null)

function refresh() {
  system.refreshHealth()
}

onMounted(refresh)
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

.sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.sec-title {
  margin: 0;
  font-size: var(--fs-body);
  font-weight: 600;
}

/* ---------------- KPI ---------------- */
.kpi-row {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: var(--gap);
}
.kpi {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 12px 14px;
  border: 1px solid var(--c-line);
  background: var(--c-surface);
}
.kpi-label {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.kpi-value {
  font-size: 20px;
  font-weight: 700;
  line-height: 1.3;
}
.kpi-hint {
  font-size: var(--fs-tiny);
}

.ok {
  color: var(--c-good);
}
.warn {
  color: var(--c-warn);
}
.danger {
  color: var(--c-danger);
}

/* ---------------- 两列 ---------------- */
.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--gap);
}

.kv {
  margin: 0;
}
.kv-row {
  display: flex;
  gap: 10px;
  padding: 5px 0;
  border-bottom: 1px solid var(--c-line-soft);
  font-size: var(--fs-small);
}
.kv-row dt {
  width: 84px;
  flex: 0 0 auto;
  color: var(--c-ink-muted);
}
.kv-row dd {
  margin: 0;
  word-break: break-all;
}

/* ---------------- 落库统计 ---------------- */
.ingest-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
  gap: 10px;
}
.ig-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--c-line);
  background: var(--c-surface-soft);
}
.ig-num {
  font-size: 19px;
  font-weight: 700;
}
.ig-label {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.ig-cell--warn .ig-num {
  color: var(--c-warn);
}
.ig-cell--danger .ig-num {
  color: var(--c-danger);
}

.offline-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.tariff-box {
  padding: 12px 14px;
  border: 1px solid var(--c-line);
  background: var(--c-surface-soft);
  font-size: var(--fs-body);
  line-height: 1.8;
  white-space: pre-wrap;
}

.side-note {
  margin: 12px 0 0;
  font-size: var(--fs-small);
  line-height: 1.8;
}

.err {
  border-color: var(--c-danger);
  color: var(--c-danger);
  font-size: var(--fs-small);
}
.err-tip {
  display: block;
  margin-top: 6px;
  color: var(--c-ink-soft);
}
.err-text {
  color: var(--c-danger);
  font-size: var(--fs-small);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.tnum {
  font-variant-numeric: tabular-nums;
}

@media (max-width: 1400px) {
  .kpi-row {
    grid-template-columns: repeat(3, 1fr);
  }
  .two-col {
    grid-template-columns: 1fr;
  }
}
</style>
