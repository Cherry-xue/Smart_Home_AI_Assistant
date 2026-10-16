<template>
  <div class="page" v-loading="system.loading && !system.health">
    <div v-if="system.healthError" class="panel err">
      无法连接后端：{{ system.healthError }}
      <div class="err-tip">
        确认 <span class="mono">backend/run_api.py</span> 已在运行，且 Vite 代理目标（默认 127.0.0.1:8000）正确。
      </div>
    </div>

    <template v-if="system.health">
      <!-- -------------------------------------------------- 概览 -->
      <div class="kpi-row">
        <StatCard label="服务状态" :value="system.health.status === 'ok' ? '正常' : '降级'" icon="Monitor" :tone="system.health.status === 'ok' ? 'ok' : 'warn'" :hint="system.health.app_env" />
        <StatCard label="数据来源" :value="system.health.data_mode" icon="Coin" :hint="dataAnchorHint" />
        <StatCard label="设备" :value="`${dev.total ?? 0}`" icon="Cpu" :hint="`在线 ${dev.online ?? 0} 台`" />
        <StatCard label="收到报文" :value="`${ingest.received ?? 0}`" icon="Message" :hint="`入库 ${ingest.readings ?? 0} 条读数`" />
        <StatCard label="丢弃/异常" :value="`${(ingest.rejected ?? 0) + (ingest.unknown_device ?? 0)}`" icon="WarnTriangleFilled" :tone="(ingest.rejected ?? 0) ? 'warn' : 'normal'" hint="拒收 + 未知设备" />
        <StatCard label="高风险自动执行" :value="system.health.high_risk_auto_exec ? '允许' : '禁止'" icon="Lock" :tone="system.health.high_risk_auto_exec ? 'danger' : 'ok'" hint="红线：恒为禁止" />
      </div>

      <div class="two-col">
        <!-- -------------------------------------------------- 数据库 -->
        <div class="panel">
          <div class="panel-title">
            数据库
            <span class="sub">{{ system.health.database.current_user }}@远程 MySQL</span>
          </div>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="连通">
              <el-tag :type="db.ok ? 'success' : 'danger'" size="small">{{ db.ok ? '正常' : '异常' }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="版本">{{ db.version ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="字符集">{{ db.charset ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="响应耗时">{{ db.response_ms === null ? '—' : `${db.response_ms} ms` }}</el-descriptions-item>
            <el-descriptions-item v-if="db.error" label="错误">
              <span class="err-text">{{ db.error }}</span>
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <!-- -------------------------------------------------- MQTT -->
        <div class="panel">
          <div class="panel-title">
            MQTT 接入
            <span class="sub">{{ mqtt.enabled ? '已启用' : '未启用' }}</span>
          </div>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="连接">
              <el-tag :type="mqtt.connected ? 'success' : 'danger'" size="small">{{ mqtt.connected ? '已连接' : '未连接' }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="订阅线程">{{ mqtt.thread_alive ? '存活' : '已退出' }}</el-descriptions-item>
            <el-descriptions-item label="运行时长">{{ mqtt.uptime_sec ? `${Math.round(mqtt.uptime_sec)} s` : '—' }}</el-descriptions-item>
            <el-descriptions-item label="会话数 / 重连">{{ mqtt.sessions ?? 0 }} / {{ mqtt.reconnects ?? 0 }}</el-descriptions-item>
            <el-descriptions-item label="已收报文">{{ mqtt.messages_seen ?? 0 }}</el-descriptions-item>
            <el-descriptions-item label="最后一条">{{ fmtDateTime(mqtt.last_message_at) }}</el-descriptions-item>
          </el-descriptions>
        </div>
      </div>

      <!-- -------------------------------------------------- 落库 -->
      <div class="panel">
        <div class="panel-title">
          数据落库统计
          <span class="sub">坏数据入库但打标，不静默丢弃</span>
        </div>
        <div class="ingest-grid">
          <div class="ig-cell"><span class="ig-num mono">{{ ingest.received ?? 0 }}</span><span class="ig-label">收到</span></div>
          <div class="ig-cell"><span class="ig-num mono">{{ ingest.readings ?? 0 }}</span><span class="ig-label">读数行</span></div>
          <div class="ig-cell"><span class="ig-num mono">{{ ingest.energies ?? 0 }}</span><span class="ig-label">能耗行</span></div>
          <div class="ig-cell"><span class="ig-num mono">{{ ingest.events ?? 0 }}</span><span class="ig-label">事件</span></div>
          <div class="ig-cell warn"><span class="ig-num mono">{{ ingest.rejected ?? 0 }}</span><span class="ig-label">拒收</span></div>
          <div class="ig-cell warn"><span class="ig-num mono">{{ ingest.unknown_device ?? 0 }}</span><span class="ig-label">未知设备</span></div>
          <div class="ig-cell warn"><span class="ig-num mono">{{ ingest.duplicates ?? 0 }}</span><span class="ig-label">重复 seq</span></div>
          <div class="ig-cell warn"><span class="ig-num mono">{{ ingest.stale ?? 0 }}</span><span class="ig-label">陈旧数据</span></div>
          <div class="ig-cell warn"><span class="ig-num mono">{{ ingest.out_of_range ?? 0 }}</span><span class="ig-label">越界</span></div>
          <div class="ig-cell"><span class="ig-num mono">{{ ingest.retained_snapshots ?? 0 }}</span><span class="ig-label">retained 快照</span></div>
          <div class="ig-cell danger"><span class="ig-num mono">{{ ingest.errors ?? 0 }}</span><span class="ig-label">入库错误</span></div>
        </div>
        <div v-if="ingest.last_error" class="err-text">{{ ingest.last_error }}</div>
      </div>

      <!-- -------------------------------------------------- 缓存与设备 -->
      <div class="two-col">
        <div class="panel">
          <div class="panel-title">
            最新值缓存
            <span class="sub">只收 good 且非陈旧的数据</span>
          </div>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="缓存设备">{{ cache.devices ?? 0 }} 台</el-descriptions-item>
            <el-descriptions-item label="缓存值">{{ cache.values ?? 0 }} 个</el-descriptions-item>
            <el-descriptions-item label="启动预热">{{ cache.preloaded ?? 0 }} 条</el-descriptions-item>
            <el-descriptions-item label="离线设备">{{ cache.offline_count ?? 0 }} 台</el-descriptions-item>
          </el-descriptions>
          <div v-if="(cache.offline_devices || []).length" class="offline-list">
            <span v-for="c in cache.offline_devices" :key="c" class="off-chip mono">{{ c }}</span>
          </div>
        </div>

        <div class="panel">
          <div class="panel-title">
            电价方案
            <span class="sub">分时电价，金额由代码计算</span>
          </div>
          <div class="tariff-box">{{ system.health.tariff }}</div>
          <div class="side-note">
            数据时间锚点：<b class="mono">{{ fmtDateTime(system.health.data_anchor) }}</b><br />
            模拟器按倍速运行，数据时间可能领先于真实时间；缺省时间窗以它为基准，
            所以"近 24 小时"指的是<b>数据里的</b>最近 24 小时。
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useSystemStore } from '@/stores/system'
import { fmtDateTime, fromNow } from '@/utils/format'
import StatCard from '@/components/StatCard.vue'

const system = useSystemStore()

const dev = computed(() => system.health?.devices ?? {})
const db = computed(() => system.health?.database ?? {})
const mqtt = computed(() => system.health?.mqtt ?? {})
const ingest = computed(() => system.health?.ingest ?? {})
const cache = computed(() => system.health?.cache ?? {})

const dataAnchorHint = computed(() => {
  const a = system.health?.data_anchor
  return a ? `锚点 ${fromNow(a)}` : '暂无数据'
})
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

.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

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
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: #101a2e;
}

.ig-num {
  font-size: 19px;
  font-weight: 700;
  color: #fff;
}

.ig-label {
  font-size: 11px;
  color: var(--text-dim);
}

.ig-cell.warn .ig-num {
  color: #ffb020;
}

.ig-cell.danger .ig-num {
  color: #ff5c6c;
}

.offline-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.off-chip {
  padding: 2px 7px;
  border-radius: 4px;
  background: #16223a;
  font-size: 11px;
  color: #8ba1c0;
}

.tariff-box {
  padding: 12px 14px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: #101a2e;
  font-size: 13px;
  line-height: 1.8;
  white-space: pre-wrap;
}

.side-note {
  margin-top: 12px;
  font-size: 12px;
  line-height: 1.8;
  color: var(--text-dim);
}

.err {
  border-color: #5a2430;
  color: #ff8b96;
}

.err-tip {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-sub);
}

.err-text {
  color: #ff8b96;
  font-size: 12px;
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
