<template>
  <div class="page">
    <!-- ---------------------------------------------------- 输入 -->
    <div class="panel">
      <div class="panel-title">
        自然语言编排
        <span class="sub">一句话 → 校验 → 剔除冲突 → 高风险留待确认</span>
      </div>

      <div class="nl-row">
        <el-input
          v-model="query"
          size="large"
          placeholder="例如：把客厅调成看电影的样子，顺便把门锁上"
          @keydown.enter="preview"
        />
        <el-button type="primary" size="large" :loading="loadingPreview" :disabled="!query.trim()" @click="preview">
          编排
        </el-button>
      </div>

      <div class="quick-row">
        <span class="qr-label">试试：</span>
        <el-button v-for="q in QUICK" :key="q" size="small" plain @click="query = q; preview()">{{ q }}</el-button>
      </div>
    </div>

    <!-- ---------------------------------------------------- 编排结果 -->
    <div v-if="result" class="panel">
      <div class="panel-title">
        编排结果
        <span class="sub">意图：{{ result.intent_label }}（{{ result.intent_key }}）</span>
      </div>

      <div v-if="result.understanding" class="understanding">{{ result.understanding }}</div>

      <div class="stat-line">
        <span>候选 <b class="mono">{{ result.raw_count }}</b></span>
        <span>保留 <b class="mono ok">{{ result.action_count }}</b></span>
        <span>剔除 <b class="mono warn">{{ result.rejected_count }}</b></span>
        <span>可执行 <b class="mono">{{ result.executable_count }}</b></span>
        <span>待确认 <b class="mono danger">{{ result.pending_confirm_count }}</b></span>
      </div>

      <el-table :data="result.actions" size="small">
        <el-table-column prop="device_name" label="设备" width="140" />
        <el-table-column prop="room_name" label="房间" width="90" />
        <el-table-column prop="capability" label="动作" width="150">
          <template #default="{ row }">
            <span class="mono">{{ row.capability }} = {{ row.value }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusMeta(row.status).type" size="small">{{ statusMeta(row.status).label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="说明" min-width="220">
          <template #default="{ row }">
            <span :class="{ 'clamp-note': row.status === 'clamped' }">{{ row.reason || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="风险" width="80" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.high_risk_device" type="danger" size="small">高风险</el-tag>
            <span v-else class="dim">{{ row.risk_level }}</span>
          </template>
        </el-table-column>
      </el-table>

      <!-- 被剔除的动作：这栏是编排层最能说明"它不是无脑照做"的地方 -->
      <div v-if="result.rejected.length" class="sec-title">被剔除的动作</div>
      <el-table v-if="result.rejected.length" :data="result.rejected" size="small">
        <el-table-column prop="device_name" label="设备" width="140" />
        <el-table-column label="动作" width="150">
          <template #default="{ row }"><span class="mono">{{ row.capability }} = {{ row.value }}</span></template>
        </el-table-column>
        <el-table-column prop="reason" label="为什么被剔除" min-width="320" />
      </el-table>

      <ul v-if="result.notes.length" class="notes">
        <li v-for="(n, i) in result.notes" :key="i">{{ n }}</li>
      </ul>

      <div class="exec-row">
        <el-button type="primary" :disabled="!result.action_count" :loading="saving" @click="saveAndRun">
          保存并执行
        </el-button>
        <span class="dim">
          执行时低风险动作直接下发，高风险动作只登记为待确认 —— 永远不会自动执行
        </span>
      </div>

      <div v-if="runOut" class="run-out">
        <div class="ro-head">
          <el-tag :type="{ success: 'success', partial: 'warning', failed: 'danger' }[runOut.status] ?? 'info'">
            {{ runOut.status }}
          </el-tag>
          <span>下发 {{ (runOut.executed || []).length }} · 待确认 {{ (runOut.pending_confirm || []).length }} · 失败 {{ (runOut.failed || []).length }}</span>
          <span v-if="runOut.fallback_used" class="dim">（有步骤失败但已继续执行其余动作）</span>
        </div>

        <el-table v-if="(runOut.pending_confirm || []).length" :data="runOut.pending_confirm" size="small" max-height="200">
          <el-table-column prop="device_name" label="待确认设备" width="150" />
          <el-table-column label="动作" width="170">
            <template #default="{ row }"><span class="mono">{{ row.capability }} = {{ row.value }}</span></template>
          </el-table-column>
          <el-table-column prop="reason" label="说明" min-width="260" />
          <el-table-column label="指令号" width="90" align="center">
            <template #default="{ row }"><span class="mono">{{ row.command_id ?? '—' }}</span></template>
          </el-table-column>
        </el-table>

        <ul v-if="runOut.notes.length" class="notes">
          <li v-for="(n, i) in runOut.notes" :key="i">{{ n }}</li>
        </ul>
      </div>
    </div>

    <!-- ---------------------------------------------------- 已保存场景 -->
    <div class="panel">
      <div class="panel-title">
        已保存场景
        <span class="sub"><el-button link size="small" @click="loadScenes">刷新</el-button></span>
      </div>
      <el-table v-loading="loadingScenes" :data="scenes" size="small">
        <el-table-column prop="name" label="场景名" min-width="160" />
        <el-table-column prop="intent_key" label="意图" width="140" />
        <el-table-column prop="action_count" label="动作数" width="80" align="center" />
        <el-table-column prop="trigger_type" label="触发" width="90" />
        <el-table-column label="启用" width="70" align="center">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '是' : '否' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :loading="runningId === row.id" @click="run(row)">执行</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createScene, listScenes, previewScene, runScene } from '@/api'
import { HOME_ID } from '@/api'
import { statusMeta } from '@/utils/format'

const QUICK = [
  '把客厅调成看电影的样子',
  '我要睡觉了',
  '出门了，把该关的都关了',
  '回家了，开灯开空调',
]

const query = ref('')
const result = ref(null)
const loadingPreview = ref(false)

const scenes = ref([])
const loadingScenes = ref(false)
const saving = ref(false)
const runOut = ref(null)
const runningId = ref(null)

async function preview() {
  const q = query.value.trim()
  if (!q) return
  loadingPreview.value = true
  runOut.value = null
  try {
    result.value = await previewScene(q)
  } catch (e) {
    ElMessage.error(`编排失败：${e.message}`)
    result.value = null
  } finally {
    loadingPreview.value = false
  }
}

async function saveAndRun() {
  if (!result.value) return
  saving.value = true
  try {
    const actions = (result.value.actions ?? [])
      .filter((a) => a.status !== 'rejected' && a.status !== 'duplicate')
      .map((a) => ({ device_code: a.device_code, capability: a.capability, value: a.value }))

    const scene = await createScene({
      home_id: HOME_ID,
      name: result.value.intent_label || query.value.slice(0, 30),
      description: query.value,
      actions,
      trigger_type: 'manual',
      enabled: true,
    })
    ElMessage.success(`场景已保存：${scene.name}`)
    await run(scene)
    await loadScenes()
  } catch (e) {
    ElMessage.error(`保存/执行失败：${e.message}`)
  } finally {
    saving.value = false
  }
}

async function run(scene) {
  runningId.value = scene.id
  try {
    runOut.value = await runScene(scene.id)
    ElMessage.success('执行完成（高风险动作仅登记为待确认）')
  } catch (e) {
    ElMessage.error(`执行失败：${e.message}`)
  } finally {
    runningId.value = null
  }
}

async function loadScenes() {
  loadingScenes.value = true
  try {
    scenes.value = (await listScenes()) ?? []
  } finally {
    loadingScenes.value = false
  }
}

onMounted(loadScenes)
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.nl-row {
  display: flex;
  gap: 10px;
}

.quick-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.qr-label {
  font-size: 12px;
  color: var(--text-dim);
}

.understanding {
  margin-bottom: 10px;
  padding: 9px 12px;
  border-left: 3px solid var(--brand);
  border-radius: 4px;
  background: #16223a;
  font-size: 13px;
  color: #c9d6e8;
}

.stat-line {
  display: flex;
  gap: 18px;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--text-sub);
}

.stat-line b {
  font-size: 14px;
  color: #fff;
}

.ok {
  color: #21d07a;
}
.warn {
  color: #ffb020;
}
.danger {
  color: #ff5c6c;
}
.dim {
  color: var(--text-dim);
}

.sec-title {
  margin: 16px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: #eaf2ff;
}

.notes {
  margin: 12px 0 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.9;
  color: var(--text-sub);
}

.exec-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px dashed #1e2b45;
  font-size: 12px;
}

.run-out {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid #2b3d5e;
  border-radius: 8px;
  background: #101a2e;
}

.ro-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  font-size: 13px;
  color: var(--text-main);
}

.clamp-note {
  color: #ffc862;
}
</style>
