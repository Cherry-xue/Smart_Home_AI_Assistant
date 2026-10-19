<template>
  <div class="ph page-fade">
    <h2 class="ph-title">{{ title }}</h2>
    <p class="ph-note">
      这一页还没做 —— 本轮只实现「房屋总览」。其余页面等设计稿。
    </p>

    <section class="ph-section">
      <h3>这页可用的后端接口</h3>
      <ul class="ph-list">
        <li v-for="e in endpoints" :key="e">
          <code>{{ e }}</code>
        </li>
      </ul>
    </section>

    <p class="ph-hint muted">
      提示：新前端与旧大屏（端口 5173）指向同一个后端，两套可以同时开着对照。
    </p>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => route.meta?.title ?? '页面')

/** 顺手把"这页将来要用哪些接口"列出来，做设计时不用再去翻后端 */
const ENDPOINT_MAP = {
  设备: ['GET /api/devices?room_id=&type=&online_only=', 'GET /api/devices/{id}', 'GET /api/devices/{id}/readings?metric=&from=&to=（⚠️ 必须传 metric，默认 24h 会 400）', 'GET /api/devices/{id}/capabilities'],
  能耗: ['GET /api/energy/summary?from=&to=', '（缺省窗口以"数据锚点"为终点，不是 now）'],
  AI对话: ['POST /api/chat', 'POST /api/chat/stream（SSE，必须用 fetch+ReadableStream）', 'GET /api/confirmations/pending', 'POST /api/confirmations/{thread_id}'],
  场景编排: ['POST /api/scenes/preview（自然语言→动作+冲突检测，会调 LLM）', 'POST /api/scenes/plan', 'GET /api/scenes', 'POST /api/scenes', 'POST /api/scenes/{scene_id}/run', 'GET /api/scenes/runs/{run_id}'],
  知识库: ['GET /api/knowledge/search?q=&k=&doc_type=', 'GET /api/knowledge/documents', 'GET /api/knowledge/status'],
  系统状态: ['GET /api/health', '（含 mqtt / ingest / cache / 数据锚点 / high_risk_auto_exec）'],
}

const endpoints = computed(() => ENDPOINT_MAP[title.value] ?? [])
</script>

<style scoped>
.ph {
  flex: 1;
  padding: 22px 26px;
  overflow-y: auto;
}
.ph-title {
  margin: 0;
  font-size: 20px;
  font-weight: 500;
}
.ph-note {
  margin: 10px 0 0;
  color: var(--c-ink-soft);
}
.ph-section {
  margin-top: 22px;
  border-top: 1px solid var(--c-line);
  padding-top: 14px;
}
.ph-section h3 {
  margin: 0 0 8px;
  font-size: var(--fs-small);
  font-weight: 500;
  color: var(--c-ink-soft);
}
.ph-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ph-list code {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.ph-hint {
  margin-top: 22px;
  font-size: var(--fs-small);
}
</style>
