<template>
  <!-- 切页动效同知识库：对话框淡入淡出、输入区从下升起、右栏从右滑入 -->
  <div class="page">
    <div class="sc-grid">
      <!-- ==================== 中间：编排对话 ==================== -->
      <div class="center-col">
        <section class="panel thread-panel page-fade">
          <div ref="listBox" class="thread">
            <div v-if="!turns.length" class="start">
              <div class="start-title">说一句话，编排成场景</div>
              <div class="start-sub muted">会先校验设备能力与冲突，越界动作被剔除并给出原因</div>
              <div class="start-chips">
                <el-button v-for="q in QUICK" :key="q" size="small" plain @click="useQuick(q)">
                  {{ q }}
                </el-button>
              </div>
            </div>

            <div v-for="t in turns" :key="t.id" class="turn">
              <div class="q-row">
                <div class="q-bubble">{{ t.q }}</div>
                <span v-if="t.kind === 'plan' && t.result" class="q-meta muted tiny">
                  {{ t.result.intent_label }}
                </span>
              </div>

              <div v-if="t.loading" class="a-block a-pending muted tiny">
                {{ t.kind === 'plan' ? '正在编排…' : '正在执行…' }}
              </div>

              <div v-else-if="t.error" class="a-block a-error">{{ t.error }}</div>

              <!-- 编排结果 -->
              <div v-else-if="t.kind === 'plan' && t.result" class="a-block">
                <div v-if="t.result.understanding" class="understanding">
                  {{ t.result.understanding }}
                </div>

                <div class="stat-line">
                  <span>候选 <b class="tnum">{{ t.result.raw_count }}</b></span>
                  <span>保留 <b class="tnum ok">{{ t.result.action_count }}</b></span>
                  <span>剔除 <b class="tnum warn">{{ t.result.rejected_count }}</b></span>
                  <span>可执行 <b class="tnum">{{ t.result.executable_count }}</b></span>
                  <span>待确认 <b class="tnum danger">{{ t.result.pending_confirm_count }}</b></span>
                </div>

                <el-table :data="t.result.actions" size="small" class="act-table">
                  <el-table-column label="设备" min-width="150">
                    <template #default="{ row }">
                      <span>{{ row.device_name }}</span>
                      <span v-if="row.high_risk_device" class="tag tag--danger hr-tag">高风险</span>
                      <div v-if="row.room_name" class="muted tiny">{{ row.room_name }}</div>
                    </template>
                  </el-table-column>
                  <el-table-column label="动作" min-width="150">
                    <template #default="{ row }">
                      <span class="mono">{{ row.capability }} = {{ row.value }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="状态" width="100">
                    <template #default="{ row }">
                      <span class="tag" :class="statusMeta(row.status).cls">
                        {{ statusMeta(row.status).label }}
                      </span>
                    </template>
                  </el-table-column>
                  <el-table-column label="说明" min-width="200">
                    <template #default="{ row }">
                      <span :class="{ 'clamp-note': row.status === 'clamped' }">
                        {{ row.reason || '—' }}
                      </span>
                    </template>
                  </el-table-column>
                </el-table>

                <!-- 被剔除的动作：编排层"不是无脑照做"的证据 -->
                <template v-if="t.result.rejected.length">
                  <h4 class="sub-title">被剔除的动作</h4>
                  <el-table :data="t.result.rejected" size="small">
                    <el-table-column prop="device_name" label="设备" min-width="150" />
                    <el-table-column label="动作" min-width="150">
                      <template #default="{ row }">
                        <span class="mono">{{ row.capability }} = {{ row.value }}</span>
                      </template>
                    </el-table-column>
                    <el-table-column prop="reason" label="为什么被剔除" min-width="240" />
                  </el-table>
                </template>

                <ul v-if="t.result.notes.length" class="notes">
                  <li v-for="(n, i) in t.result.notes" :key="i">{{ n }}</li>
                </ul>

                <div class="exec-row">
                  <!-- 只存不跑：编排出来的东西未必想立刻下发，先存进右栏随时再执行 -->
                  <el-button
                    size="small"
                    :disabled="!t.result.action_count"
                    :loading="t.savingOnly"
                    @click="saveTurn(t, false)"
                  >
                    保存
                  </el-button>
                  <el-button
                    type="primary"
                    size="small"
                    :disabled="!t.result.action_count"
                    :loading="t.saving"
                    @click="saveTurn(t, true)"
                  >
                    保存并执行
                  </el-button>
                </div>

                <p v-if="t.saved" class="saved-tip">{{ t.saved }}</p>
              </div>

              <!-- 执行回执（编排后执行 / 右栏点执行，都落到这里） -->
              <div v-if="t.receipt" class="a-block run-out">
                <div class="ro-head">
                  <span class="tag" :class="runTagClass(t.receipt.status)">
                    {{ runStatusLabel(t.receipt.status) }}
                  </span>
                  <span class="tnum">
                    下发 {{ (t.receipt.executed || []).length }} · 待确认
                    {{ (t.receipt.pending_confirm || []).length }} · 失败
                    {{ (t.receipt.failed || []).length }}
                  </span>
                  <span v-if="t.receipt.fallback_used" class="muted tiny">
                    （有步骤失败但已继续执行其余动作）
                  </span>
                </div>

                <el-table
                  v-if="(t.receipt.pending_confirm || []).length"
                  :data="t.receipt.pending_confirm"
                  size="small"
                  max-height="200"
                >
                  <el-table-column prop="device_name" label="待确认设备" min-width="140" />
                  <el-table-column label="动作" min-width="150">
                    <template #default="{ row }">
                      <span class="mono">{{ row.capability }} = {{ row.value }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column prop="reason" label="说明" min-width="200" />
                  <el-table-column label="指令号" width="80" align="center">
                    <template #default="{ row }">
                      <span class="mono">{{ row.command_id ?? '—' }}</span>
                    </template>
                  </el-table-column>
                </el-table>

                <ul v-if="(t.receipt.notes || []).length" class="notes">
                  <li v-for="(n, i) in t.receipt.notes" :key="i">{{ n }}</li>
                </ul>
              </div>
            </div>
          </div>
        </section>

        <!-- ==================== 输入区 ==================== -->
        <section class="panel composer-panel page-slide">
          <div class="input-row">
            <el-input
              v-model="query"
              size="large"
              placeholder="例如：把客厅调成看电影的样子，顺便把门锁上"
              @keydown.enter="preview"
            />
            <el-button
              type="primary"
              size="large"
              :loading="loadingPreview"
              :disabled="!query.trim()"
              @click="preview"
            >
              编排
            </el-button>
          </div>
        </section>
      </div>

      <!-- ==================== 右栏：已保存的场景 ==================== -->
      <aside class="side-col page-slide-right">
        <section class="panel side-block">
          <header class="sec-head">
            <h3 class="sec-title">已保存的场景</h3>
            <span class="chip tnum">{{ scenes.length }}</span>
            <button class="mini-btn" :class="{ spinning: loadingScenes }" title="刷新" @click="loadScenes">
              刷新
            </button>
          </header>

          <div class="scene-list">
            <!-- 整张卡片可点：点开编辑动作；按钮各自 stop，避免误触 -->
            <article v-for="s in scenes" :key="s.id" class="scene-card" @click="openEditor(s)">
              <div class="sc-row">
                <span class="sc-name">{{ s.name }}</span>
                <span class="tag" :class="s.enabled ? 'tag--good' : ''">
                  {{ s.enabled ? '启用' : '停用' }}
                </span>
              </div>

              <div class="sc-meta muted tiny">
                {{ s.intent_key }} · {{ s.action_count }} 个动作 · {{ s.trigger_type }}
              </div>

              <!-- 删除与执行、收藏同一行；靠右对齐，正好落在上方「启用」标签的正下方 -->
              <div class="sc-actions">
                <button class="sc-btn" :disabled="runningId === s.id" @click.stop="runSaved(s)">
                  {{ runningId === s.id ? '执行中…' : '执行' }}
                </button>
                <button
                  class="sc-btn"
                  :class="{ 'sc-btn--on': s.favorite }"
                  :title="s.favorite ? '取消收藏（总览不再显示）' : '收藏后显示在房屋总览'"
                  @click.stop="toggleFavorite(s)"
                >
                  {{ s.favorite ? '已收藏' : '收藏' }}
                </button>
                <button
                  class="sc-btn sc-btn--del"
                  title="永久删除这个场景及其执行记录"
                  @click.stop="removeScene(s)"
                >
                  删除
                </button>
              </div>
            </article>

            <p v-if="!scenes.length" class="empty">还没有保存过场景</p>
          </div>
        </section>
      </aside>
    </div>

    <!-- ==================== 编辑场景 ==================== -->
    <el-dialog
      v-model="editorOpen"
      title="编辑场景"
      width="860px"
      top="8vh"
      append-to-body
      :close-on-click-modal="false"
      class="scene-editor"
    >
      <div class="ed-field">
        <label>名称</label>
        <el-input v-model="editorName" maxlength="64" show-word-limit />
      </div>
      <div class="ed-field">
        <label>描述</label>
        <el-input v-model="editorDesc" placeholder="这句话是在什么情况下说的（可留空）" />
      </div>

      <div class="ed-head">
        <span>动作（{{ editorRows.length }}）</span>
        <button class="mini-btn" @click="addRow">添加动作</button>
      </div>

      <!-- 只有动作列表滚动：名称、描述、按钮栏都固定在原位。
           高度 = 视口 - 上下留白 - 标题栏 - 按钮栏 - 名称/描述两行，取不到就按 240px 兜底。 -->
      <div ref="edBody" class="ed-body">
      <p v-if="!editorRows.length" class="empty ed-empty">没有动作的场景不能保存</p>

      <div v-for="(row, i) in editorRows" :key="i" class="ed-row">
        <el-select
          v-model="row.device_code"
          filterable
          placeholder="设备"
          class="ed-dev"
          @change="onDeviceChange(row)"
        >
          <el-option
            v-for="d in deviceOptions"
            :key="d.code"
            :label="`${d.room_name || ''} / ${d.name}`"
            :value="d.code"
          />
        </el-select>

        <el-select
          v-model="row.capability"
          placeholder="能力"
          class="ed-cap"
          @change="onCapChange(row)"
        >
          <el-option
            v-for="c in capsFor(row.device_code)"
            :key="c.code"
            :label="c.display_name || c.code"
            :value="c.code"
          />
        </el-select>

        <!-- 值控件按能力的 value_type 走：枚举给下拉、数值给数字框、其余给文本框 -->
        <el-select v-if="capOf(row)?.value_type === 'bool'" v-model="row.value" class="ed-val">
          <el-option label="开（true）" :value="true" />
          <el-option label="关（false）" :value="false" />
        </el-select>

        <el-select
          v-else-if="capOf(row)?.value_type === 'enum'"
          v-model="row.value"
          class="ed-val"
          filterable
        >
          <el-option v-for="v in capOf(row).enum_values" :key="v" :label="v" :value="v" />
        </el-select>

        <el-input-number
          v-else-if="isNumeric(row)"
          v-model="row.value"
          class="ed-val"
          :min="capOf(row).value_min ?? undefined"
          :max="capOf(row).value_max ?? undefined"
          controls-position="right"
        />

        <el-input v-else v-model="row.value" class="ed-val" placeholder="值" />

        <button class="ed-del" title="删除这条动作" @click="editorRows.splice(i, 1)">×</button>
      </div>
      </div><!-- /.ed-body -->

      <!-- 校验不通过的回执固定显示，不跟着动作列表滚走 -->
      <div v-if="editorRejected.length" class="ed-rejected">
        <p class="ed-rej-title">这些动作没通过校验，整次改动没有保存：</p>
        <ul>
          <li v-for="(r, i) in editorRejected" :key="i">
            {{ r.device_name || r.device_code }} · {{ r.capability }} = {{ r.value }}
            <span class="muted">— {{ r.reason }}</span>
          </li>
        </ul>
      </div>

      <template #footer>
        <el-button @click="editorOpen = false">取消</el-button>
        <el-button type="primary" :loading="editorSaving" @click="saveEditor">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createScene,
  deleteScene,
  getDeviceCapabilities,
  getDevices,
  listScenes,
  previewScene,
  runScene,
  setSceneFavorite,
  updateScene,
} from '@/api'
import { useHomeStore } from '@/stores/home'
import { statusMeta } from '@/utils/format'

const home = useHomeStore()

const QUICK = ['把客厅调成看电影的样子', '我要睡觉了', '出门了，该关的都关了', '回家了，开灯开空调']

const RUN_STATUS_LABEL = { success: '已完成', partial: '部分执行', failed: '失败' }
const runStatusLabel = (s) => RUN_STATUS_LABEL[s] ?? s ?? '—'
function runTagClass(s) {
  if (s === 'success') return 'tag--good'
  if (s === 'partial') return 'tag--warn'
  if (s === 'failed') return 'tag--danger'
  return ''
}

const query = ref('')
const loadingPreview = ref(false)
const turns = ref([])
const listBox = ref(null)

const scenes = ref([])
const loadingScenes = ref(false)
const runningId = ref(null)

let seq = 0

/** 滚到底：新结果可能比可视区高，不滚就看不出"有新内容" */
function scrollBottom() {
  nextTick(() => {
    if (listBox.value) listBox.value.scrollTop = listBox.value.scrollHeight
  })
}

function useQuick(q) {
  query.value = q
  preview()
}

/**
 * 编排。
 *
 * 每轮自己带 `result / receipt / saving`，不再共用全局状态 ——
 * 否则上一轮刚执行完的回执，会被下一轮的 loading 顶掉。
 */
async function preview() {
  const q = query.value.trim()
  if (!q || loadingPreview.value) return

  const turn = {
    id: `t${seq++}`,
    kind: 'plan',
    q,
    loading: true,
    error: '',
    result: null,
    receipt: null,
    saving: false,
    savingOnly: false,
    saved: '',
  }
  turns.value.push(turn)
  query.value = ''
  loadingPreview.value = true
  scrollBottom()

  try {
    // 带上当前套房：后端按它收窄设备目录，否则会编排到别的套房的设备上
    turn.result = await previewScene(q, false, home.currentId)
  } catch (e) {
    turn.error = `编排失败：${e.message}`
  } finally {
    turn.loading = false
    loadingPreview.value = false
    scrollBottom()
  }
}

/**
 * 把这一轮编排结果存成场景。
 *
 * run=true  → 保存后立刻执行，回执进对话流
 * run=false → 只保存，进右栏「已保存的场景」，什么时候执行由人决定
 *
 * 两个按钮各自一个 loading（saving / savingOnly），
 * 否则点「保存」会把「保存并执行」一起转圈。
 */
async function saveTurn(t, run) {
  if (!t.result) return
  const flag = run ? 'saving' : 'savingOnly'
  t[flag] = true
  t.saved = ''
  try {
    // 被剔除/被覆盖的动作不进场景：它们本来就没通过校验
    const actions = (t.result.actions ?? [])
      .filter((a) => a.status !== 'rejected' && a.status !== 'duplicate')
      .map((a) => ({ device_code: a.device_code, capability: a.capability, value: a.value }))

    const scene = await createScene({
      home_id: home.currentId,
      name: t.result.intent_label || t.q.slice(0, 30),
      description: t.q,
      actions,
      trigger_type: 'manual',
      enabled: true,
    })

    if (run) {
      t.receipt = await runScene(scene.id)
    } else {
      t.saved = `已保存为「${scene.name}」，在右栏点执行或点卡片编辑动作`
    }
    await loadScenes()
  } catch (e) {
    t.error = `${run ? '保存/执行' : '保存'}失败：${e.message}`
  } finally {
    t[flag] = false
    scrollBottom()
  }
}

/** 右栏点执行：结果进对话流，右栏列表不塞长表格 */
async function runSaved(s) {
  if (runningId.value) return
  const turn = {
    id: `t${seq++}`,
    kind: 'run',
    q: `执行场景「${s.name}」`,
    loading: true,
    error: '',
    result: null,
    receipt: null,
    saving: false,
  }
  turns.value.push(turn)
  runningId.value = s.id
  scrollBottom()

  try {
    turn.receipt = await runScene(s.id)
  } catch (e) {
    turn.error = `执行失败：${e.message}`
  } finally {
    turn.loading = false
    runningId.value = null
    scrollBottom()
  }
}

async function loadScenes() {
  loadingScenes.value = true
  try {
    scenes.value = (await listScenes(home.currentId)) ?? []
  } catch {
    scenes.value = []
  } finally {
    loadingScenes.value = false
  }
}

// ---------------------------------------------------------------- 收藏 / 删除
async function toggleFavorite(s) {
  const next = !s.favorite
  try {
    const out = await setSceneFavorite(s.id, next)
    // 就地替换：换整份列表会让卡片闪一下、丢失滚动位置
    Object.assign(s, out)
    ElMessage({
      message: next ? `已收藏「${s.name}」，房屋总览会显示它` : `已取消收藏「${s.name}」`,
      type: 'success',
      duration: 2200,
    })
  } catch (e) {
    ElMessage.error(`收藏失败：${e.message}`)
  }
}

async function removeScene(s) {
  const cnt = s.action_count
  try {
    await ElMessageBox.confirm(
      `「${s.name}」及其 ${cnt} 个动作、全部执行记录都会被永久删除，无法恢复。`,
      '删除场景',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return // 用户取消
  }

  try {
    const r = await deleteScene(s.id)
    scenes.value = scenes.value.filter((x) => x.id !== s.id)
    ElMessage.success(
      r?.runs_deleted
        ? `已删除「${r.name}」（含 ${r.runs_deleted} 条执行记录）`
        : `已删除「${r?.name ?? s.name}」`,
    )
  } catch (e) {
    ElMessage.error(`删除失败：${e.message}`)
  }
}

// ---------------------------------------------------------------- 编辑弹窗
const editorOpen = ref(false)
const editorScene = ref(null)
const editorName = ref('')
const editorDesc = ref('')
const editorRows = ref([])
const editorSaving = ref(false)
const editorRejected = ref([])
const edBody = ref(null)

const devices = ref([])
/** device_code → 可写能力数组（懒加载，每台只取一次） */
const capsByCode = reactive({})

const deviceByCode = computed(() => Object.fromEntries(devices.value.map((d) => [d.code, d])))

/**
 * 设备下拉项。
 *
 * 默认只列**有可写能力**的设备 —— 传感器放进来只会让下拉变成一长串无选项。
 * 但已在场景里的设备即便不可写也要保留，否则那一行会显示成空白。
 */
const deviceOptions = computed(() => {
  const list = devices.value.filter((d) => (d.writable_count ?? 0) > 0)
  const known = new Set(list.map((d) => d.code))
  const extra = editorRows.value
    .map((r) => deviceByCode.value[r.device_code])
    .filter((d) => d && !known.has(d.code))
  return [...list, ...extra]
})

function capsFor(code) {
  return capsByCode[code] ?? []
}

function capOf(row) {
  return capsFor(row.device_code).find((c) => c.code === row.capability)
}

function isNumeric(row) {
  const t = capOf(row)?.value_type
  return t === 'int' || t === 'float'
}

async function ensureDevices() {
  if (devices.value.length) return
  try {
    const r = await getDevices({ home_id: home.currentId })
    devices.value = r?.items ?? []
  } catch {
    devices.value = []
  }
}

async function ensureCaps(code) {
  if (!code || capsByCode[code]) return
  const dev = deviceByCode.value[code]
  if (!dev) {
    capsByCode[code] = []
    return
  }
  try {
    const r = await getDeviceCapabilities(dev.id)
    // 只留可写能力：场景动作是"要下发"的，读类能力放进来只会被后端剔除
    capsByCode[code] = (r?.capabilities ?? []).filter((c) => c.writable)
  } catch {
    capsByCode[code] = []
  }
}

/** 换设备：能力与值都作废，不能把上一台设备的 capability 留在这一行 */
function onDeviceChange(row) {
  row.capability = ''
  row.value = null
  ensureCaps(row.device_code)
}

/** 换能力：给一个合法初值，免得保存时因为 null 被后端剔除 */
function onCapChange(row) {
  const cap = capOf(row)
  if (!cap) {
    row.value = null
    return
  }
  if (cap.value_type === 'bool') row.value = true
  else if (cap.value_type === 'enum') row.value = cap.enum_values?.[0] ?? null
  else if (cap.value_type === 'int' || cap.value_type === 'float') {
    row.value = cap.value_min ?? 0
  } else row.value = ''
}

async function openEditor(s) {
  editorScene.value = s
  editorName.value = s.name
  editorDesc.value = s.description || ''
  editorRows.value = (s.actions ?? []).map((a) => ({
    device_code: a.device_code,
    capability: a.capability,
    value: a.value,
  }))
  editorRejected.value = []
  editorOpen.value = true
  // 复用同一个弹窗：上次的滚动位置不该带到这次
  nextTick(() => {
    if (edBody.value) edBody.value.scrollTop = 0
  })

  await ensureDevices()
  // 逐台补能力档案：编辑器要按 value_type 选控件，没有档案就只能出文本框
  await Promise.all([...new Set(editorRows.value.map((r) => r.device_code))].map(ensureCaps))
}

function addRow() {
  editorRows.value.push({ device_code: '', capability: '', value: null })
  // 新行加在末尾，滚到底才看得见（动作多时列表是内部滚动的）
  nextTick(() => {
    if (edBody.value) edBody.value.scrollTop = edBody.value.scrollHeight
  })
}

async function saveEditor() {
  if (!editorScene.value) return
  if (!editorName.value.trim()) {
    ElMessage.warning('名称不能为空')
    return
  }
  if (!editorRows.value.length) {
    ElMessage.warning('场景至少要有一个动作')
    return
  }
  const bad = editorRows.value.find((r) => !r.device_code || !r.capability)
  if (bad) {
    ElMessage.warning('有动作还没选设备或能力')
    return
  }

  editorSaving.value = true
  editorRejected.value = []
  try {
    const out = await updateScene(editorScene.value.id, {
      name: editorName.value.trim(),
      description: editorDesc.value.trim(),
      actions: editorRows.value.map((r) => ({
        device_code: r.device_code,
        capability: r.capability,
        value: r.value,
      })),
    })
    const idx = scenes.value.findIndex((x) => x.id === out.id)
    if (idx >= 0) scenes.value[idx] = out
    editorOpen.value = false
    ElMessage.success('场景已保存')
  } catch (e) {
    // 422 的 detail 是 {message, rejected}，把被剔除的动作原样列出来
    const detail = e.payload?.detail
    if (detail && typeof detail === 'object' && Array.isArray(detail.rejected)) {
      editorRejected.value = detail.rejected
      ElMessage.warning(detail.message || '有动作没通过校验')
    } else {
      ElMessage.error(`保存失败：${e.message}`)
    }
  } finally {
    editorSaving.value = false
  }
}

// 换套房就换一批场景：场景是挂在 home_id 上的；设备缓存也必须清
watch(
  () => home.currentId,
  () => {
    turns.value = []
    scenes.value = []
    devices.value = []
    Object.keys(capsByCode).forEach((k) => delete capsByCode[k])
    loadScenes()
  },
)

onMounted(loadScenes)
</script>

<style scoped>
.page {
  padding: var(--pad);
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.sc-grid {
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
  margin-top: 24px;
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

.understanding {
  padding: 9px 12px;
  border-left: 3px solid var(--c-accent);
  background: var(--c-accent-soft);
  font-size: var(--fs-body);
  color: var(--c-ink-soft);
}

.stat-line {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin: 10px 0;
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.stat-line b {
  font-size: var(--fs-body);
  color: var(--c-ink);
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

.act-table {
  margin-top: 4px;
}
.hr-tag {
  margin-left: 6px;
}

.sub-title {
  margin: 16px 0 8px;
  font-size: var(--fs-body);
  font-weight: 600;
}

.notes {
  margin: 10px 0 0;
  padding-left: 18px;
  font-size: var(--fs-small);
  line-height: 1.9;
  color: var(--c-ink-soft);
}

.exec-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}

.saved-tip {
  margin: 8px 0 0;
  font-size: var(--fs-small);
  color: var(--c-good);
}

.clamp-note {
  color: var(--c-warn);
}

.run-out {
  padding: 12px;
  border: 1px solid var(--c-line);
  background: var(--c-surface-soft);
}
.ro-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
  font-size: var(--fs-body);
}

/* ---------------- 输入区 ---------------- */
.composer-panel {
  flex: 0 0 auto;
}
.input-row {
  display: flex;
  gap: 10px;
}

/* ---------------- 右栏：场景卡片 ---------------- */
.side-col {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.side-block {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
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
.mini-btn {
  margin-left: auto;
  padding: 2px 8px;
  border: 1px solid var(--c-line);
  background: transparent;
  font-size: var(--fs-tiny);
  color: var(--c-ink-soft);
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.mini-btn:hover {
  color: var(--c-accent);
  border-color: var(--c-accent);
}

.scene-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  /* 滚动条往右让 6px，与知识库右栏一致 */
  margin-right: -6px;
  padding-right: 6px;
}

.scene-card {
  margin-bottom: 10px;
  padding: 10px 12px;
  border: 1px solid var(--c-line);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.scene-card:hover {
  border-color: var(--c-accent);
  background: var(--c-surface-soft);
}

.sc-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.sc-name {
  font-size: var(--fs-small);
  font-weight: 600;
  line-height: 1.5;
  word-break: break-word;
}
.sc-meta {
  margin-top: 3px;
  line-height: 1.6;
}

.sc-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

/* 方框小按钮：和线框图里的「执行 / 收藏 / 删除」一致 */
.sc-btn {
  padding: 2px 10px;
  border: 1px solid var(--c-line);
  background: transparent;
  font-size: var(--fs-tiny);
  color: var(--c-ink-soft);
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.sc-btn:hover:not(:disabled) {
  color: var(--c-accent);
  border-color: var(--c-accent);
}
.sc-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.sc-btn--on {
  color: var(--c-accent);
  border-color: var(--c-accent);
  background: var(--c-accent-soft);
}

/* 删除：与执行、收藏同一行（默认 align-items: stretch，三者等高）；
   横向内距与上方「启用」标签一致（6px），红字红框；
   margin-left:auto 把它推到行尾，正好落在「启用」的正下方。 */
.sc-btn--del {
  margin-left: auto;
  padding: 1px 6px;
  border-color: var(--c-danger);
  color: var(--c-danger);
}
.sc-btn--del:hover:not(:disabled) {
  color: #fff;
  border-color: var(--c-danger);
  background: var(--c-danger);
}

/* ---------------- 编辑弹窗 ---------------- */
.scene-editor :deep(.el-dialog__body) {
  padding-top: 10px;
  padding-bottom: 10px;
}

/* 动作列表的专属滚动区：名称、描述、标题栏、按钮栏都不参与滚动。
   高度 = 视口 - 弹窗上下留白(16vh) - 标题栏 - 按钮栏 - 名称/描述 - 动作小标题，
   再留一点余量；动作再长也只是这里滚，弹窗总高不超视口。 */
.ed-body {
  max-height: max(120px, calc(100vh - 380px));
  overflow-y: auto;
  /* 负 margin + 等量 padding：只把滚动条往右挪 4px，内容排版不动 */
  margin-right: -4px;
  padding-right: 4px;
}

.ed-field {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.ed-field label {
  flex: 0 0 44px;
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}

.ed-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 16px 0 8px;
  font-size: var(--fs-body);
  font-weight: 600;
}
.ed-head .mini-btn {
  margin-left: 0;
}

.ed-empty {
  padding: 16px 0;
}

.ed-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.ed-dev {
  flex: 1 1 auto;
  min-width: 0;
}
.ed-cap {
  flex: 0 0 180px;
}
.ed-val {
  flex: 0 0 160px;
}
.ed-del {
  flex: 0 0 26px;
  height: 26px;
  border: 1px solid var(--c-line);
  background: transparent;
  color: var(--c-ink-muted);
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
}
.ed-del:hover {
  color: var(--c-danger);
  border-color: var(--c-danger);
}

.ed-rejected {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid var(--c-danger);
  background: #fdecec;
  font-size: var(--fs-small);
}
.ed-rej-title {
  margin: 0 0 6px;
  color: var(--c-danger);
}
.ed-rejected ul {
  margin: 0;
  padding-left: 18px;
  line-height: 1.8;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.tnum {
  font-variant-numeric: tabular-nums;
}

@media (max-width: 1280px) {
  .sc-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .side-col {
    display: none;
  }
}
</style>
