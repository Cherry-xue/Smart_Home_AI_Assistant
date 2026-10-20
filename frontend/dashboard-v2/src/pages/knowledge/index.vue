<template>
  <!-- 切页动效（规则见 styles/transitions.css）：
       对话框 `.page-fade` 淡入淡出；底部输入区 `.page-slide` 从下方升起 / 向下滑出；
       右侧栏 `.page-slide-right` 从右侧滑入 / 向右滑出。
       根节点刻意**不挂** page-fade —— 否则整页再淡一层，滑动会显得"糊"。 -->
  <div class="page">
    <div class="kb-grid">
      <!-- ==================== 中间：问答区 ==================== -->
      <div class="center-col">
        <section class="panel thread-panel page-fade">
          <div ref="listBox" class="thread">
            <div v-if="!turns.length" class="start">
              <div class="start-title">问说明书里的事</div>
              <div class="start-sub muted">答案只来自已入库的说明书与应急预案，每条都带原文出处</div>
              <div class="start-chips">
                <el-button v-for="s in QUICK" :key="s" size="small" plain @click="useQuick(s)">
                  {{ s }}
                </el-button>
              </div>
            </div>

            <div v-for="t in turns" :key="t.id" class="turn">
              <div class="q-row">
                <div class="q-bubble">{{ t.q }}</div>
                <span class="q-meta muted tiny">
                  {{ t.k }} 条<template v-if="t.docType"> · {{ docTypeLabel(t.docType) }}</template><template
                    v-if="t.deviceType"
                  >
                    · {{ t.deviceType }}</template
                  >
                </span>
              </div>

              <div v-if="t.loading" class="a-block a-pending muted tiny">检索中…</div>

              <div v-else-if="t.error" class="a-block a-error">{{ t.error }}</div>

              <div v-else class="a-block">
                <header class="a-head">
                  <span class="a-count">{{ t.out.count }} 条命中</span>
                  <span class="muted tiny tnum">
                    {{ t.out.elapsed_ms }}ms · 向量库 {{ t.out.collection_size }} 块
                  </span>
                </header>

                <p v-if="!t.out.hits.length" class="empty">没有召回到相关片段</p>

                <article v-for="(h, i) in t.out.hits" :key="i" class="hit">
                  <div class="hit-head">
                    <span class="hit-idx mono">#{{ i + 1 }}</span>
                    <span class="hit-doc">{{ h.doc_title }}</span>
                    <span class="tag" :class="h.doc_type === 'emergency_plan' ? 'tag--danger' : 'tag--accent'">
                      {{ docTypeLabel(h.doc_type) }}
                    </span>
                    <span class="hit-dist mono tnum">
                      距离 {{ h.distance === null ? '—' : Number(h.distance).toFixed(3) }}
                    </span>
                  </div>

                  <div class="hit-section">{{ h.section }}</div>
                  <div class="hit-content">{{ h.content }}</div>

                  <div class="hit-foot">
                    <span class="hf-src mono">{{ h.source_file }}</span>
                    <span v-for="t2 in h.device_types" :key="t2" class="tag">{{ t2 }}</span>
                  </div>
                </article>
              </div>
            </div>
          </div>
        </section>

        <!-- ==================== 输入区 ==================== -->
        <section class="panel composer-panel page-slide">
          <div class="input-row">
            <el-input
              v-model="q"
              size="large"
              placeholder="例如：客厅空调滤网多久清洗一次 / 燃气泄漏了怎么办"
              @keydown.enter="search"
            />
            <el-button type="primary" size="large" :loading="loading" :disabled="!q.trim()" @click="search">
              发送
            </el-button>
          </div>

          <div class="param-row">
            <span class="pr-label">返回条数</span>
            <el-slider v-model="k" :min="1" :max="10" show-stops class="pr-slider" />
            <span class="pr-val tnum">{{ k }}</span>

            <span class="pr-label">文档类型</span>
            <el-select v-model="docType" clearable placeholder="不限" class="pr-select">
              <el-option label="设备说明书" value="device_manual" />
              <el-option label="应急预案" value="emergency_plan" />
            </el-select>

            <span class="pr-label">设备类型</span>
            <el-select v-model="deviceType" clearable filterable placeholder="不限" class="pr-select">
              <el-option v-for="t in DEVICE_TYPES" :key="t" :label="t" :value="t" />
            </el-select>
          </div>
        </section>
      </div>

      <!-- ==================== 右侧栏 ==================== -->
      <aside class="side-col page-slide-right">
        <section class="panel side-block side-docs">
          <header class="sec-head">
            <h3 class="sec-title">已入库文档</h3>
            <span class="chip tnum">{{ docs.length }} 份</span>
          </header>

          <div class="doc-list">
            <article v-for="(d, i) in docs" :key="d.doc_title || i" class="doc">
              <div class="doc-top">
                <span class="doc-title">{{ d.doc_title }}</span>
                <span class="tag" :class="d.doc_type === 'emergency_plan' ? 'tag--danger' : 'tag--accent'">
                  {{ d.doc_type === 'emergency_plan' ? '预案' : '说明书' }}
                </span>
              </div>
              <div class="doc-meta muted tiny tnum">{{ d.chunks }} 块</div>
              <div class="doc-sections muted tiny">{{ (d.sections || []).join(' / ') || '—' }}</div>
            </article>

            <SkeletonList v-if="loading && !docs.length" :rows="4" :bar-h="16" />
            <p v-else-if="!docs.length" class="empty">还没有入库文档</p>
          </div>
        </section>

        <section class="panel side-block side-status">
          <header class="sec-head">
            <h3 class="sec-title">数据库状态</h3>
          </header>

          <dl v-if="status" class="kv">
            <div class="kv-row"><dt>集合</dt><dd class="mono">{{ status.collection }}</dd></div>
            <div class="kv-row"><dt>向量块</dt><dd class="tnum">{{ status.chunks }}</dd></div>
            <div class="kv-row"><dt>已入库</dt><dd class="tnum">{{ status.indexed_documents }} 份</dd></div>
            <div class="kv-row"><dt>嵌入模型</dt><dd class="mono">{{ status.embedding_model }}</dd></div>
            <div class="kv-row"><dt>源文件</dt><dd class="tnum">{{ status.raw_files }} 个</dd></div>
          </dl>
          <SkeletonList v-else-if="loading" :rows="5" :bar-h="12" />
          <p v-else class="empty">读不到状态</p>

          <p v-if="status?.note" class="kb-note">{{ status.note }}</p>
        </section>
      </aside>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import SkeletonList from '@/components/SkeletonList.vue'
import { getKnowledgeStatus, listDocuments, searchKnowledge } from '@/api'

const QUICK = [
  '客厅空调滤网多久清洗一次',
  '燃气泄漏了怎么办',
  '门锁电池没电了怎么应急开门',
  '扫地机器人多久清理一次尘盒',
]

/**
 * 设备类型下拉项。
 *
 * ⚠️ `/api/knowledge/documents` 的分档里**没有** device_types 字段
 * （只有 hits 里才有），所以这里只能列一份常备清单 —— 它只用来做过滤候选，
 * 清单过期不会报错，只是过滤不到东西。
 */
const DEVICE_TYPES = [
  'light', 'ac', 'tv', 'curtain', 'door_lock', 'gas_sensor', 'gas_valve',
  'smoke_sensor', 'water_sensor', 'temp_sensor', 'camera', 'robot_vacuum',
  'air_purifier', 'water_heater', 'breaker',
]

const q = ref('')
const k = ref(4)
const docType = ref(null)
const deviceType = ref(null)
const loading = ref(true)   // 首帧即骨架，避免淡入时先显示"还没有入库文档"
const turns = ref([])
const listBox = ref(null)

const docs = ref([])
const status = ref(null)

let seq = 0

function docTypeLabel(t) {
  if (t === 'emergency_plan') return '应急预案'
  if (t === 'device_manual') return '说明书'
  return t || '—'
}

function scrollBottom() {
  nextTick(() => {
    if (listBox.value) listBox.value.scrollTop = listBox.value.scrollHeight
  })
}

function useQuick(s) {
  q.value = s
  search()
}

async function search() {
  const text = q.value.trim()
  if (!text || loading.value) return

  // 参数按"提问那一刻"的快照记下来：问完再改滑块，历史那条不该跟着变
  const turn = {
    id: `t${seq++}`,
    q: text,
    k: k.value,
    docType: docType.value || null,
    deviceType: deviceType.value || null,
    loading: true,
    out: null,
    error: '',
  }
  turns.value.push(turn)
  q.value = ''
  loading.value = true
  scrollBottom()

  try {
    turn.out = await searchKnowledge(text, {
      k: turn.k,
      doc_type: turn.docType || undefined,
      device_type: turn.deviceType || undefined,
    })
  } catch (e) {
    turn.error = `检索失败：${e.message}`
    ElMessage({ message: turn.error, type: 'error', duration: 4500 })
  } finally {
    turn.loading = false
    loading.value = false
    scrollBottom()
  }
}

async function loadMeta() {
  // 两个请求一起发、一起收尾：`loading` 的初值是 true（首屏骨架），
  // 必须等**文档列表和数据库状态都回来**才能置 false ——
  // 否则状态卡会先闪一下"读不到状态"。
  try {
    // ⚠️ 这个端点返回的是**数组**（response_model=list[IndexedDocumentOut]），
    //    不是 {documents: [...]} —— 别顺手套一层 .documents
    const [d, st] = await Promise.all([
      listDocuments().catch(() => []),
      getKnowledgeStatus().catch(() => null),
    ])
    docs.value = d ?? []
    status.value = st
  } finally {
    loading.value = false
  }
}

onMounted(loadMeta)
</script>

<style scoped>
.page {
  padding: var(--pad);
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.kb-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: var(--gap);
  height: 100%;
  min-height: 0;
}

.panel {
  background: var(--c-surface);
  border: 1px solid var(--c-line);
  padding: var(--pad);
}

/* ---------------- 中间列 ---------------- */
.center-col {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  min-width: 0;
  min-height: 0;
}

.thread-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
}
.thread {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--pad);
}

.start {
  padding: 56px 0;
  text-align: center;
}
.start-title {
  font-size: 15px;
}
.start-sub {
  margin-top: 6px;
  font-size: var(--fs-small);
}
.start-chips {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 18px;
}

.turn + .turn {
  margin-top: 22px;
}

.q-row {
  display: flex;
  align-items: baseline;
  justify-content: flex-end;
  gap: 8px;
}
.q-bubble {
  max-width: 80%;
  padding: 9px 13px;
  border: 1px solid var(--c-accent);
  background: var(--c-accent-soft);
  font-size: 13.5px;
  line-height: 1.7;
  word-break: break-word;
}
.q-meta {
  flex: 0 0 auto;
}

.a-block {
  margin-top: 12px;
}
.a-pending {
  padding: 4px 0;
}
.a-error {
  padding: 8px 10px;
  background: #fdecec;
  font-size: var(--fs-small);
  color: var(--c-danger);
}

.a-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--c-line-soft);
}
.a-count {
  font-size: var(--fs-body);
  font-weight: 600;
}

/* ---------------- 命中片段 ---------------- */
.hit {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid var(--c-line);
  background: var(--c-surface-soft);
}
.hit-head {
  display: flex;
  align-items: center;
  gap: 9px;
}
.hit-idx {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.hit-doc {
  font-size: var(--fs-body);
  font-weight: 600;
}
.hit-dist {
  margin-left: auto;
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}
.hit-section {
  margin: 6px 0;
  font-size: var(--fs-small);
  color: var(--c-accent);
}
.hit-content {
  font-size: var(--fs-body);
  line-height: 1.8;
  white-space: pre-wrap;
}
.hit-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 9px;
  padding-top: 8px;
  border-top: 1px dashed var(--c-line);
}
.hf-src {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}

.kb-note {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--c-line);
  font-size: var(--fs-tiny);
  line-height: 1.7;
  color: var(--c-ink-muted);
}

/* ---------------- 输入区 ---------------- */
.composer-panel {
  flex: 0 0 auto;
}
.input-row {
  display: flex;
  gap: 10px;
}
.param-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 12px;
}
.pr-label {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.pr-slider {
  width: 150px;
}
.pr-val {
  width: 14px;
  font-size: var(--fs-small);
}
.pr-select {
  width: 132px;
}

/* ---------------- 右侧栏 ---------------- */
.side-col {
  display: grid;
  grid-template-rows: minmax(0, 1.5fr) minmax(0, 1fr);
  gap: var(--gap);
  min-height: 0;
}
.side-block {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: var(--pad);
}
.side-docs {
  overflow: hidden;
}
.doc-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  /* 滚动条往右让 6px：负 margin 把滚动容器右边界推到内边距里，
     padding 再把文字拉回原位 —— 只动滚动条，文字与「说明书」标签位置不变。
     ⚠️ 两个数值必须相等，否则文字会跟着挪。 */
  margin-right: -6px;
  padding-right: 6px;
}

.sec-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
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

.doc {
  padding: 9px 0;
  border-bottom: 1px solid var(--c-line-soft);
}
.doc:last-child {
  border-bottom: none;
}
.doc-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.doc-title {
  font-size: var(--fs-small);
  font-weight: 600;
  line-height: 1.5;
}
.doc-meta {
  margin-top: 3px;
}
.doc-sections {
  margin-top: 2px;
  font-size: var(--fs-tiny);
  line-height: 1.6;
}

.kv {
  margin: 0;
  overflow-y: auto;
}
.kv-row {
  display: flex;
  gap: 10px;
  padding: 5px 0;
  border-bottom: 1px solid var(--c-line-soft);
  font-size: var(--fs-small);
}
.kv-row dt {
  width: 64px;
  flex: 0 0 auto;
  color: var(--c-ink-muted);
}
.kv-row dd {
  margin: 0;
  word-break: break-all;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.tnum {
  font-variant-numeric: tabular-nums;
}

@media (max-width: 1280px) {
  .kb-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .side-col {
    display: none;
  }
}
</style>
