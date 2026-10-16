<template>
  <div class="shell">
    <!-- ------------------------------------------------ 侧边栏 -->
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-logo">SH</div>
        <div class="brand-text">
          <div class="brand-title">智能家居 AI</div>
          <div class="brand-sub">态势大屏</div>
        </div>
      </div>

      <el-menu
        :default-active="route.path"
        router
        background-color="transparent"
        text-color="#8ba1c0"
        active-text-color="#2ea8ff"
      >
        <el-menu-item v-for="r in menuRoutes" :key="r.path" :index="r.path">
          <el-icon><component :is="r.meta.icon" /></el-icon>
          <template #title>{{ r.meta.title }}</template>
        </el-menu-item>
      </el-menu>

      <!-- 红线提示常驻：任何时候都能看见"高风险永不自动执行" -->
      <div class="redline">
        <el-icon><Lock /></el-icon>
        <div>
          高风险设备<br />永不自动执行
        </div>
      </div>
    </aside>

    <!-- ------------------------------------------------ 主区 -->
    <div class="main">
      <header class="topbar">
        <div class="tb-left">
          <h2>{{ route.meta.title }}</h2>
          <span class="tb-sub">{{ pageSubtitle }}</span>
        </div>

        <div class="tb-right">
          <!-- 数据锚点：模拟数据的时间轴不在"现在"，必须显式告诉看屏的人 -->
          <div class="tb-item" :title="anchorTip">
            <span class="tb-label">数据时间</span>
            <span class="mono tb-value">{{ anchorText }}</span>
            <span v-if="isSimulated" class="mock-badge">模拟</span>
          </div>

          <el-divider direction="vertical" />

          <div class="tb-item">
            <span class="tag-dot" :class="system.online ? 'dot-ok' : 'dot-danger'" />
            <span>{{ system.online ? '服务正常' : '服务异常' }}</span>
          </div>

          <el-badge :value="system.pendingCount" :hidden="system.pendingCount === 0" :max="9">
            <el-button size="small" :type="system.pendingCount ? 'warning' : 'default'" @click="gotoPending">
              <el-icon><Bell /></el-icon>
              <span style="margin-left: 4px">待确认</span>
            </el-button>
          </el-badge>

          <el-button size="small" circle :loading="system.loading" @click="system.refreshAll()">
            <el-icon><Refresh /></el-icon>
          </el-button>
        </div>
      </header>

      <section class="content">
        <router-view v-slot="{ Component }">
          <component :is="Component" :key="route.fullPath" />
        </router-view>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { pageRoutes } from '@/router'
import { useSystemStore } from '@/stores/system'
import { fmtDateTime, fromNow } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const system = useSystemStore()

// 侧栏菜单直接由路由表生成，加页面不用改两处
const menuRoutes = pageRoutes

const anchorText = computed(() => fmtDateTime(system.dataAnchor))
// data_mode 不是 real_gateway，就意味着数据来自模拟器 —— 界面上必须标出来
const isSimulated = computed(() => (system.health?.data_mode ?? '') !== 'real_gateway')

const anchorTip = computed(() => {
  const a = system.dataAnchor
  if (!a) return '后端还没有数据'
  return `手上最新数据的观测时间：${a}（${fromNow(a)}）。模拟器按倍速运行，该时间可能领先于真实时间。`
})

const pageSubtitle = computed(() => {
  // ⚠️ health.devices 的键是 total / rooms / high_risk / online —— 没有 devices 这个键
  const d = system.deviceStats
  if (!d || !d.total) return ''
  return `在线 ${d.online ?? 0} / ${d.total} 台 · 房间 ${d.rooms ?? 0} 间 · 高风险 ${d.high_risk ?? 0} 台`
})

function gotoPending() {
  router.push({ path: '/chat', query: { tab: 'pending' } })
}

onMounted(() => {
  system.refreshAll()
  system.startPolling()
})

onUnmounted(() => system.stopPolling())
</script>

<style scoped>
.shell {
  display: flex;
  height: 100%;
  background: var(--bg-base);
}

/* ------------------------------------------------ 侧栏 */
.sidebar {
  display: flex;
  flex-direction: column;
  width: 190px;
  flex: 0 0 190px;
  padding: 18px 12px;
  background: #0a1120;
  border-right: 1px solid var(--border-soft);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 8px 20px;
}

.brand-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  background: linear-gradient(135deg, #2ea8ff, #00e5c0);
  color: #04101f;
  font-weight: 800;
  font-size: 14px;
}

.brand-title {
  font-size: 15px;
  font-weight: 700;
  color: #eaf2ff;
  letter-spacing: 1px;
}

.brand-sub {
  font-size: 11px;
  color: var(--text-dim);
  letter-spacing: 2px;
}

.redline {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: auto;
  padding: 10px;
  border: 1px solid #3a2030;
  border-radius: 8px;
  background: #1a0f18;
  font-size: 11px;
  line-height: 1.5;
  color: #d98a9a;
}

/* ------------------------------------------------ 主区 */
.main {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 62px;
  padding: 0 20px;
  background: #0a1120;
  border-bottom: 1px solid var(--border-soft);
}

.tb-left {
  display: flex;
  align-items: baseline;
  gap: 12px;
  min-width: 0;
}

.tb-left h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #eaf2ff;
  letter-spacing: 1px;
}

.tb-sub {
  font-size: 12px;
  color: var(--text-dim);
}

.tb-right {
  display: flex;
  align-items: center;
  gap: 14px;
}

.tb-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-sub);
}

.tb-label {
  color: var(--text-dim);
}

.tb-value {
  color: #eaf2ff;
}

.content {
  flex: 1;
  overflow: auto;
  padding: 18px 20px 28px;
}
</style>
