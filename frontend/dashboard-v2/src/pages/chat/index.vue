<template>
  <div class="page page-fade">
    <!-- ==================== 顶部 ==================== -->
    <div class="panel top-strip">
      <div class="ts-left">
        <span class="ts-title">AI 对话</span>
        <span class="muted tiny">
          会话 <span class="mono">{{ threadId }}</span>
        </span>
      </div>
      <div class="ts-right">
        <el-button size="small" :disabled="!messages.length" @click="newSession">新会话</el-button>
      </div>
    </div>

    <div class="grid">
      <!-- ==================== 对话区 ==================== -->
      <section class="panel chat-main">
        <div ref="msgBox" class="msgs">
          <div v-if="!messages.length" class="start-hint">
            <div class="sh-title">直接说你想做什么</div>
            <div class="sh-sub muted">
              例如「把客厅调成看电影的样子」「现在家里耗电情况怎么样」「燃气泄漏了怎么办」
            </div>
            <div class="sh-chips">
              <el-button v-for="q in QUICK" :key="q" size="small" plain @click="useQuick(q)">
                {{ q }}
              </el-button>
            </div>
          </div>

          <div v-for="m in messages" :key="m.id" class="msg" :class="`role-${m.role}`">
            <div v-if="m.role === 'user'" class="bubble user-bubble">{{ m.text }}</div>

            <div v-else class="bubble ai-bubble">
              <!-- 执行过程：流式到达时逐步增长 -->
              <div v-if="m.trace.length" class="trace">
                <span class="trace-label">
                  {{ m.streaming ? '执行中' : `已完成 ${m.trace.length} 步` }}
                </span>
                <el-collapse v-if="m.trace.length" class="trace-collapse">
                  <el-collapse-item>
                    <template #title><span class="tc-title">查看执行过程</span></template>
                    <div v-for="(t, i) in m.trace" :key="i" class="trace-item">
                      <span class="ti-node mono">{{ t.node }}</span>
                      <span class="ti-label">{{ t.label }}</span>
                      <span class="ti-detail muted">{{ t.detail }}</span>
                      <span v-if="t.elapsed_ms" class="ti-ms mono tnum">
                        {{ Math.round(t.elapsed_ms) }}ms
                      </span>
                    </div>
                  </el-collapse-item>
                </el-collapse>
              </div>

              <div v-if="m.answer" class="answer">{{ m.answer }}</div>
              <div v-else-if="m.streaming" class="answer muted">正在处理…</div>

              <AnswerSections
                v-if="Object.keys(m.sections || {}).length"
                :sections="m.sections"
                class="ai-sections"
              />

              <!-- 高风险 / 冲突：只有人点了才继续 -->
              <InterventionCard
                v-for="(item, i) in m.interventions"
                :key="`${m.id}-${i}`"
                :item="{ ...item, thread_id: m.threadId }"
                :index="i"
                @decided="onDecided"
              />

              <div v-if="m.runResult" class="run-result">
                <span class="tag" :class="runTagClass(m.runResult.status)">
                  {{ runStatusLabel(m.runResult.status) }}
                </span>
                <span>下发 {{ (m.runResult.executed || []).length }}</span>
                <span>待确认 {{ (m.runResult.pending_confirm || []).length }}</span>
                <span>失败 {{ (m.runResult.failed || []).length }}</span>
                <span v-if="m.runResult.fallback_used" class="muted">（有步骤失败但已继续）</span>
              </div>

              <div v-if="m.error" class="msg-error">{{ m.error }}</div>
            </div>
          </div>
        </div>

        <!-- ==================== 输入区 ==================== -->
        <div class="composer">
          <el-input
            v-model="input"
            type="textarea"
            :rows="2"
            resize="none"
            placeholder="说一句话，例如：把客厅调成看电影的样子，顺便把门锁上"
            @keydown.enter.exact.prevent="send"
          />
          <div class="composer-bar">
            <span class="cb-hint muted tiny">Enter 发送 · Shift+Enter 换行</span>
            <el-button v-if="streaming" size="small" type="danger" @click="stop">停止</el-button>
            <el-button
              v-else
              size="small"
              type="primary"
              :loading="sending"
              :disabled="!input.trim()"
              @click="send"
            >
              发送
            </el-button>
          </div>
        </div>
      </section>

      <!-- ==================== 侧栏：待确认 ==================== -->
      <aside class="panel side">
        <header class="sec-head">
          <h3 class="sec-title">待确认事项</h3>
          <span v-if="pendingCount" class="chip chip--warn">{{ pendingCount }}</span>
        </header>

        <p v-if="!pendingCount" class="empty">当前没有待确认事项</p>

        <InterventionCard
          v-for="(item, i) in pending"
          :key="`${item.thread_id}-${i}`"
          :item="item"
          :index="i"
          @decided="onDecided"
        />
      </aside>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { chatStream } from '@/api'
import { useHomeStore } from '@/stores/home'
import { useSystemStore } from '@/stores/system'
import AnswerSections from '@/components/AnswerSections.vue'
import InterventionCard from '@/components/InterventionCard.vue'

const home = useHomeStore()
const system = useSystemStore()

const QUICK = [
  '把客厅调成看电影的样子',
  '现在家里耗电情况怎么样',
  '主卧现在温度和湿度是多少',
  '燃气泄漏了怎么办',
]

const input = ref('')
const sending = ref(false)
const streaming = ref(false)
const messages = ref([])
const msgBox = ref(null)
const threadId = ref(newThreadId())

const pending = computed(() => system.pending ?? [])
const pendingCount = computed(() => pending.value.length)

const RUN_STATUS_LABEL = {
  success: '已完成',
  partial: '部分执行',
  failed: '失败',
  rejected: '已驳回',
}
const runStatusLabel = (s) => RUN_STATUS_LABEL[s] ?? s ?? '—'
function runTagClass(s) {
  if (s === 'success') return 'tag--good'
  if (s === 'partial') return 'tag--warn'
  if (s === 'failed') return 'tag--danger'
  return ''
}

let seq = 0
let abortFn = null

function newThreadId() {
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
}

function newSession() {
  stop()
  threadId.value = newThreadId()
  messages.value = []
  input.value = ''
}

function useQuick(q) {
  input.value = q
  send()
}

function scrollBottom() {
  nextTick(() => {
    if (msgBox.value) msgBox.value.scrollTop = msgBox.value.scrollHeight
  })
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return

  const tid = threadId.value
  const userMsg = { id: `m${seq++}`, role: 'user', text }
  const aiMsg = {
    id: `m${seq++}`,
    role: 'assistant',
    threadId: tid,
    streaming: true,
    trace: [],
    sections: {},
    interventions: [],
    answer: '',
    runResult: null,
    error: '',
  }
  messages.value.push(userMsg, aiMsg)
  input.value = ''
  streaming.value = true
  sending.value = true
  scrollBottom()

  const { promise, abort } = chatStream(text, tid, home.currentId, {
    onEvent: (ev) => handleEvent(ev, aiMsg),
  })
  abortFn = abort

  try {
    await promise
  } catch (e) {
    aiMsg.error = `请求失败：${e.message}`
  } finally {
    aiMsg.streaming = false
    streaming.value = false
    sending.value = false
    abortFn = null
    await system.refreshPending()
    scrollBottom()
  }
}

function handleEvent(ev, msg) {
  switch (ev.type) {
    case 'start':
      if (ev.thread_id) msg.threadId = ev.thread_id
      break

    case 'node':
      msg.trace.push({
        node: ev.node,
        label: ev.label,
        detail: ev.detail,
        elapsed_ms: ev.elapsed_ms,
      })
      scrollBottom()
      break

    case 'interrupt':
      // 挂起等人：卡片由 InterventionCard 渲染，批准/驳回走 /confirmations
      msg.interventions = ev.payload?.interventions ?? []
      msg.streaming = true
      system.refreshPending()
      scrollBottom()
      break

    case 'final': {
      const r = ev.result || {}
      msg.answer = r.answer || ''
      msg.sections = r.answer_sections ?? {}
      msg.runResult = r.run_result ?? null
      if (r.trace?.length) msg.trace = r.trace
      if (r.intervention?.interventions) msg.interventions = r.intervention.interventions
      scrollBottom()
      break
    }

    case 'error':
      msg.error = ev.message ?? '出错了'
      break

    default:
      break
  }
}

function stop() {
  abortFn?.()
  abortFn = null
  streaming.value = false
  sending.value = false
  const last = [...messages.value].reverse().find((m) => m.role === 'assistant' && m.streaming)
  if (last) last.streaming = false
}

async function onDecided({ choice, out }) {
  await system.refreshPending()
  if (!out?.answer) return
  const verb = choice === 'approve' ? '已批准' : '已驳回'
  messages.value.push({
    id: `m${seq++}`,
    role: 'assistant',
    threadId: out.thread_id,
    streaming: false,
    trace: [],
    sections: {},
    interventions: [],
    answer: `${verb} → ${out.answer}`,
    runResult: out.run_result ?? null,
    error: '',
  })
  scrollBottom()
}

onMounted(() => system.refreshPending())
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  padding: var(--pad);
  height: 100%;
  min-height: 0;
  overflow: hidden;
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
  gap: 10px;
}

.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: var(--gap);
  flex: 1;
  min-height: 0;
}

/* ---------------- 对话区 ---------------- */
.chat-main {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.msgs {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-right: 6px;
}

.start-hint {
  padding: 40px 0;
  text-align: center;
}
.sh-title {
  font-size: 15px;
}
.sh-sub {
  margin-top: 6px;
  font-size: var(--fs-small);
}
.sh-chips {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
}

.msg {
  display: flex;
  margin-bottom: 14px;
}
.role-user {
  justify-content: flex-end;
}

.bubble {
  max-width: 78%;
  padding: 11px 14px;
  font-size: 13.5px;
  line-height: 1.7;
  word-break: break-word;
}
.user-bubble {
  border: 1px solid var(--c-accent);
  background: var(--c-accent-soft);
  color: var(--c-ink);
}
.ai-bubble {
  border: 1px solid var(--c-line);
  background: var(--c-surface-soft);
}

/* ---------------- 执行过程 ---------------- */
.trace {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.trace-collapse {
  flex: 1;
  border: none;
}
.tc-title {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.trace-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 0;
  font-size: var(--fs-small);
  border-top: 1px solid var(--c-line-soft);
}
.ti-node {
  color: var(--c-accent);
}
.ti-detail {
  flex: 1;
}
.ti-ms {
  color: var(--c-ink-muted);
}

.answer {
  font-size: var(--fs-body);
  font-weight: 600;
  line-height: 1.7;
}
.ai-sections {
  margin-top: 10px;
}

.run-result {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--c-line);
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}

.msg-error {
  margin-top: 8px;
  padding: 8px 10px;
  background: #fdecec;
  font-size: var(--fs-small);
  color: var(--c-danger);
}

/* ---------------- 输入区 ---------------- */
.composer {
  margin-top: 12px;
}
.composer-bar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 8px;
}
.cb-hint {
  margin-right: auto;
}

/* ---------------- 侧栏 ---------------- */
.side {
  overflow-y: auto;
}
.sec-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.sec-title {
  margin: 0;
  font-size: var(--fs-body);
  font-weight: 600;
}
.chip {
  padding: 1px 7px;
  border: 1px solid var(--c-line);
  font-size: var(--fs-tiny);
  color: var(--c-ink-soft);
}
.chip--warn {
  border-color: var(--c-warn);
  color: var(--c-warn);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.tnum {
  font-variant-numeric: tabular-nums;
}

@media (max-width: 1280px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .side {
    display: none;
  }
}
</style>
