<template>
  <div class="chat-page">
    <!-- -------------------------------------------------- 对话区 -->
    <div class="panel chat-main">
      <div class="panel-title">
        AI 助手对话
        <span class="sub">
          会话 <span class="mono">{{ threadId }}</span>
          <el-button link size="small" @click="newSession">新会话</el-button>
        </span>
      </div>

      <div ref="msgBox" class="msgs">
        <div v-if="!messages.length" class="start-hint">
          <el-icon :size="26"><ChatDotRound /></el-icon>
          <div class="sh-title">直接说你想做什么</div>
          <div class="sh-sub">
            例如「把客厅调成看电影的样子」「现在家里耗电情况怎么样」「燃气泄漏了怎么办」
          </div>
          <div class="sh-chips">
            <el-button v-for="q in QUICK" :key="q" size="small" plain @click="useQuick(q)">{{ q }}</el-button>
          </div>
        </div>

        <div v-for="m in messages" :key="m.id" class="msg" :class="`role-${m.role}`">
          <!-- 用户 -->
          <template v-if="m.role === 'user'">
            <div class="bubble user-bubble">{{ m.text }}</div>
          </template>

          <!-- 助手 -->
          <template v-else>
            <div class="bubble ai-bubble">
              <!-- 执行过程（流式时实时增长） -->
              <div v-if="m.trace.length" class="trace">
                <el-icon v-if="m.streaming" class="is-spin"><Loading /></el-icon>
                <el-icon v-else><CircleCheckFilled /></el-icon>
                <span class="trace-label">{{ m.streaming ? '执行中' : `已完成 ${m.trace.length} 步` }}</span>
                <el-collapse v-if="m.trace.length" class="trace-collapse">
                  <el-collapse-item>
                    <template #title><span class="tc-title">查看执行过程</span></template>
                    <div v-for="(t, i) in m.trace" :key="i" class="trace-item">
                      <span class="ti-node mono">{{ t.node }}</span>
                      <span class="ti-label">{{ t.label }}</span>
                      <span class="ti-detail">{{ t.detail }}</span>
                      <span v-if="t.elapsed_ms" class="ti-ms mono">{{ Math.round(t.elapsed_ms) }}ms</span>
                    </div>
                  </el-collapse-item>
                </el-collapse>
              </div>

              <!-- 结论 -->
              <div v-if="m.answer" class="answer">{{ m.answer }}</div>
              <div v-else-if="m.streaming" class="answer dim">正在处理…</div>

              <AnswerSections v-if="Object.keys(m.sections || {}).length" :sections="m.sections" class="ai-sections" />

              <!-- 待确认卡片（高风险 / 冲突裁决共用） -->
              <InterventionCard
                v-for="(item, i) in m.interventions"
                :key="`${m.id}-${i}`"
                :item="{ ...item, thread_id: m.threadId }"
                :index="i"
                @decided="onDecided"
              />

              <!-- 执行结果 -->
              <div v-if="m.runResult" class="run-result">
                <el-tag size="small" :type="runTagType(m.runResult.status)">{{ m.runResult.status }}</el-tag>
                <span>下发 {{ (m.runResult.executed || []).length }}</span>
                <span>待确认 {{ (m.runResult.pending_confirm || []).length }}</span>
                <span>失败 {{ (m.runResult.failed || []).length }}</span>
                <span v-if="m.runResult.fallback_used" class="dim">（有步骤失败但已继续）</span>
              </div>

              <div v-if="m.error" class="msg-error">{{ m.error }}</div>
            </div>
          </template>
        </div>
      </div>

      <!-- -------------------------------------------------- 输入区 -->
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
          <span class="cb-hint">Enter 发送 · Shift+Enter 换行</span>
          <el-button v-if="streaming" type="danger" size="small" @click="stop">停止</el-button>
          <el-button v-else type="primary" size="small" :loading="sending" :disabled="!input.trim()" @click="send">
            发送
          </el-button>
        </div>
      </div>
    </div>

    <!-- -------------------------------------------------- 待确认侧栏 -->
    <aside class="panel chat-side">
      <div class="panel-title">
        待确认事项
        <span class="sub">
          <el-badge :value="system.pendingCount" :hidden="!system.pendingCount" :max="9" />
        </span>
      </div>

      <div v-if="!system.pendingCount" class="empty-box">当前没有待确认事项</div>

      <InterventionCard
        v-for="(item, i) in system.pending"
        :key="`${item.thread_id}-${i}`"
        :item="item"
        :index="i"
        @decided="onDecided"
      />

      <div class="side-note">
        高风险动作只在<b>这里</b>弹窗等人确认；低风险动作会先执行再告知。
        确认有效期默认 {{ TTL }} 分钟，超时自动作废。
      </div>
    </aside>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'
import { chatStream } from '@/api'
import { useSystemStore } from '@/stores/system'
import AnswerSections from '@/components/AnswerSections.vue'
import InterventionCard from '@/components/InterventionCard.vue'

const route = useRoute()
const system = useSystemStore()

const QUICK = [
  '把客厅调成看电影的样子',
  '现在家里耗电情况怎么样',
  '主卧现在温度和湿度是多少',
  '出门了，把该关的都关了，顺便锁门',
  '燃气泄漏了怎么办',
]

const TTL = 15 // 与后端 CONFIRM_TTL_MIN 保持一致（展示用）

const input = ref('')
const sending = ref(false)
const streaming = ref(false)
const messages = ref([])
const msgBox = ref(null)
const threadId = ref(newThreadId())

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

function runTagType(status) {
  return { success: 'success', partial: 'warning', failed: 'danger' }[status] ?? 'info'
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return

  const tid = threadId.value
  const userMsg = { id: `m${seq++}`, role: 'user', text, time: new Date() }
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
    time: new Date(),
  }
  messages.value.push(userMsg, aiMsg)
  input.value = ''
  streaming.value = true
  sending.value = true
  scrollBottom()

  const { promise, abort } = chatStream(text, tid, { onEvent: (ev) => handleEvent(ev, aiMsg) })
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
      msg.trace.push({ node: ev.node, label: ev.label, detail: ev.detail, elapsed_ms: ev.elapsed_ms })
      scrollBottom()
      break

    case 'interrupt': {
      const payload = ev.payload || {}
      msg.interventions = payload.interventions ?? []
      msg.streaming = true
      system.refreshPending()
      scrollBottom()
      break
    }

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
  if (out?.answer) {
    messages.value.push({
      id: `m${seq++}`,
      role: 'assistant',
      threadId: out.thread_id,
      streaming: false,
      trace: [],
      sections: {},
      interventions: [],
      answer: `${choice === 'approve' || choice === '同意' ? '已批准' : '已驳回'} → ${out.answer}`,
      runResult: out.run_result ?? null,
      error: '',
      time: new Date(),
    })
    scrollBottom()
  }
}

// 从别的页面点"待确认"跳进来时，滚一下并刷新列表
watch(
  () => route.query.tab,
  (v) => {
    if (v === 'pending') system.refreshPending()
  },
  { immediate: true },
)

onMounted(() => system.refreshPending())
</script>

<style scoped>
.chat-page {
  display: grid;
  grid-template-columns: 1fr 340px;
  gap: 16px;
  height: calc(100vh - 62px - 46px);
}

.chat-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding-bottom: 12px;
}

.msgs {
  flex: 1;
  overflow-y: auto;
  padding-right: 6px;
}

.start-hint {
  padding: 46px 0;
  text-align: center;
  color: var(--text-dim);
}

.sh-title {
  margin-top: 8px;
  font-size: 15px;
  color: #c9d6e8;
}

.sh-sub {
  margin-top: 6px;
  font-size: 12px;
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
  border-radius: 10px;
  font-size: 13.5px;
  line-height: 1.7;
  word-break: break-word;
}

.user-bubble {
  background: linear-gradient(135deg, #1d4f7a, #17405f);
  color: #eaf2ff;
}

.ai-bubble {
  background: #101a2e;
  border: 1px solid var(--border-soft);
  color: var(--text-main);
}

/* -------------------------------------------------- 执行过程 */
.trace {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--text-sub);
}

.is-spin {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.trace-collapse {
  flex: 1;
  border: none;
}

.tc-title {
  font-size: 11px;
  color: var(--text-dim);
}

.trace-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 0;
  font-size: 11.5px;
  border-top: 1px dashed #1e2b45;
}

.ti-node {
  color: #00e5c0;
}

.ti-label {
  color: #c9d6e8;
}

.ti-detail {
  flex: 1;
  color: var(--text-dim);
}

.ti-ms {
  color: var(--text-dim);
}

/* -------------------------------------------------- 结论 */
.answer {
  font-size: 14px;
  font-weight: 600;
  color: #fff;
  line-height: 1.7;
}

.answer.dim {
  font-weight: 400;
  color: var(--text-dim);
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
  border-top: 1px dashed #1e2b45;
  font-size: 12px;
  color: var(--text-sub);
}

.msg-error {
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(255, 92, 108, 0.1);
  font-size: 12px;
  color: #ff8b96;
}

/* -------------------------------------------------- 输入区 */
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
  font-size: 11px;
  color: var(--text-dim);
}

/* -------------------------------------------------- 侧栏 */
.chat-side {
  overflow-y: auto;
}

.side-note {
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px dashed #1e2b45;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-dim);
}

@media (max-width: 1280px) {
  .chat-page {
    grid-template-columns: 1fr;
    height: auto;
  }
}
</style>
