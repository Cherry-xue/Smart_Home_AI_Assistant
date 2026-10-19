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
        <span class="muted tiny">历史会话 {{ sessions.length }} 个</span>
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
                <el-collapse v-if="m.trace.length" class="trace-collapse">
                  <el-collapse-item>
                    <!-- 两段文字都放进标题插槽：Element 的 header 本身是 flex + align-items:center，
                         这样"已完成 N 步"和"查看执行过程"天然按中线对齐，不用两头各算一遍行高 -->
                    <template #title>
                      <span class="trace-label">
                        {{ m.streaming ? '执行中' : `已完成 ${m.trace.length} 步` }}
                      </span>
                      <span class="tc-title">查看执行过程</span>
                    </template>
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
        <!-- `page-slide`：切页时离开下沉、进入从下方升起（与知识库页一致） -->
        <div class="composer page-slide">
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

      <!-- ==================== 历史会话 ==================== -->
      <aside class="panel side page-slide-right">
        <el-button class="new-btn" @click="newSession">新会话</el-button>

        <div class="hist-list">
          <p v-if="!sessions.length" class="empty">
            还没有历史会话。<br />说一句话，这里就会留下记录。
          </p>

          <div
            v-for="s in sessions"
            :key="s.thread_id"
            class="hist-item"
            :class="{ 'hist-item--active': s.thread_id === threadId }"
            @click="openSession(s)"
          >
            <div class="hi-title">{{ s.title }}</div>
            <div class="hi-meta muted tiny tnum">
              {{ fmtDateTime(s.last_at) }} · {{ s.message_count }} 条
            </div>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { chatStream, listChatMessages, listChatSessions } from '@/api'
import { useHomeStore } from '@/stores/home'
import { useSystemStore } from '@/stores/system'
import { fmtDateTime } from '@/utils/format'
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

// 确认只在两处出现：聊天流里的卡片 + 顶栏「消息」抽屉。
// 右侧那栏改成了「历史会话」（原来放待确认事项，与聊天流重复）。

/** 历史会话列表（最近说过的排前面） */
const sessions = ref([])
/** 正在回放的会话，避免重复点同一个反复请求 */
const loadingSession = ref('')

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

/** 拉一次历史会话列表。失败只提示，不打断对话（历史是附加信息）。 */
async function loadSessions() {
  try {
    sessions.value = await listChatSessions(home.currentId)
  } catch {
    /* 静默：列表拉不到不影响发消息 */
  }
}

/** 后端一条消息 → 页面气泡。字段名两侧不同，集中在这里转换，别散在模板里 */
function toMessage(row) {
  if (row.role === 'user') {
    return { id: `h${seq++}`, role: 'user', text: row.content }
  }
  return {
    id: `h${seq++}`,
    role: 'assistant',
    threadId: threadId.value,
    streaming: false,
    trace: row.trace ?? [],
    sections: row.sections ?? {},
    // 历史消息里的待确认早就处理过了，不再画确认卡（避免又点一次）
    interventions: [],
    answer: row.content ?? '',
    runResult: null,
    error: '',
  }
}

/** 点开一条历史会话：切 thread_id 并回放消息。 */
async function openSession(s) {
  if (streaming.value) {
    ElMessage.warning('正在回答，等这一轮结束再切换会话')
    return
  }
  if (s.thread_id === threadId.value && messages.value.length) return
  if (loadingSession.value) return

  loadingSession.value = s.thread_id
  threadId.value = s.thread_id
  messages.value = []
  try {
    const rows = await listChatMessages(s.thread_id)
    messages.value = rows.map(toMessage)
    scrollBottom()
  } catch (e) {
    ElMessage.error(`读取历史会话失败：${e.message}`)
  } finally {
    loadingSession.value = ''
  }
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
    // 这一轮已经落库，刷新列表让新会话（或标题）立刻出现在右侧
    await loadSessions()
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

onMounted(() => {
  system.refreshPending()
  loadSessions()
})

// 历史会话是按套房存的。切套房后必须重拉列表，并把当前对话清空 ——
// 否则会把 A 套房的上下文接着当成 B 套房在聊。
watch(
  () => home.currentId,
  () => {
    newSession()
    loadSessions()
  },
)
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

/* 左：对话区；右：历史会话。待确认事项不再单开一栏（聊天流里已经有了） */
.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 220px;
  gap: var(--gap);
  flex: 1;
  min-height: 0;
}

@media (max-width: 1280px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .side {
    display: none;
  }
}

/* ---------------- 对话区 ---------------- */
.chat-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
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
/* 整块（左侧「已完成 N 步」+ 右侧折叠区）共用一块底、一道边框。
   以前折叠区是 Element 自带的白底，左边那段却露着气泡的浅灰底，
   一块区域切出两种颜色，很难看。 */
.trace {
  margin-bottom: 10px;
  padding: 6px 10px;
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
  background: var(--c-surface);
  border: 1px solid var(--c-line);
  border-radius: 2px;
}
.trace-label {
  color: var(--c-ink-soft);
}
.trace-collapse {
  border: none;
  background: transparent;
}
/* 抹平 Element Plus 折叠面板自带的白底与上下分隔线，底部只留 .trace 那一道边框 */
.trace-collapse :deep(.el-collapse-item__header),
.trace-collapse :deep(.el-collapse-item__wrap) {
  background: transparent;
  border-bottom: none;
}
/* Element Plus 用的是 min-height: 48px（不是 height），只写 height:auto 压不下来。
   header 自带 `display:flex; align-items:center`，两段文字放进去就是按中线对齐的 */
.trace-collapse :deep(.el-collapse-item__header) {
  height: 20px;
  min-height: 20px;
  line-height: 20px;
  padding: 0;
  gap: 8px;
}
/* 标题插槽外面还包了一层 `.el-collapse-item__title`（默认 `flex:auto` 的行内盒），
   两段文字在里面是按基线排的 —— 字号一大一小就成了"底部对齐"。改成 flex 才按中线对齐 */
.trace-collapse :deep(.el-collapse-item__title) {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* 箭头保持贴右（默认是 `margin: 0 8px 0 auto`，只改左外边距会把箭头拽到标题旁边） */
.trace-collapse :deep(.el-collapse-item__arrow) {
  margin: 0 0 0 auto;
}
.trace-collapse :deep(.el-collapse-item__content) {
  padding-bottom: 0;
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

/* ---------------- 历史会话 ---------------- */
.side {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
  overflow: hidden;
}
.new-btn {
  width: 100%;
}
.hist-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  /* 滚动条贴到面板内边距上，不压住卡片内容 */
  margin-right: -6px;
  padding-right: 6px;
}
.hist-item {
  padding: 8px 9px;
  border: 1px solid transparent;
  border-bottom-color: var(--c-line-soft);
  cursor: pointer;
  transition: background 120ms var(--ease);
}
.hist-item:hover {
  background: var(--c-surface-hover);
}
.hist-item--active {
  border-color: var(--c-accent);
  background: var(--c-accent-soft);
}
.hi-title {
  font-size: var(--fs-small);
  color: var(--c-ink);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.hi-meta {
  margin-top: 3px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.tnum {
  font-variant-numeric: tabular-nums;
}
</style>
