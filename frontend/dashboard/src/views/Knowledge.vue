<template>
  <div class="page">
    <!-- ---------------------------------------------------- 检索 -->
    <div class="panel">
      <div class="panel-title">
        说明书 / 预案检索
        <span class="sub">向量召回，只给片段不编造 —— 引用必须能指到文件与章节</span>
      </div>

      <div class="search-row">
        <el-input
          v-model="q"
          size="large"
          placeholder="例如：客厅空调滤网多久清洗一次 / 燃气泄漏了怎么办"
          @keydown.enter="search"
        />
        <el-button type="primary" size="large" :loading="loading" :disabled="!q.trim()" @click="search">检索</el-button>
      </div>

      <div class="filter-row">
        <span class="fr-label">返回条数</span>
        <el-slider v-model="k" :min="1" :max="10" show-stops style="width: 180px" />
        <span class="mono">{{ k }}</span>

        <span class="fr-label">文档类型</span>
        <el-select v-model="docType" clearable placeholder="不限" style="width: 160px">
          <el-option label="设备说明书" value="device_manual" />
          <el-option label="应急预案" value="emergency_plan" />
        </el-select>

        <span class="fr-label">设备类型</span>
        <el-select v-model="deviceType" clearable filterable placeholder="不限" style="width: 160px">
          <el-option v-for="t in deviceTypes" :key="t" :label="t" :value="t" />
        </el-select>
      </div>

      <div class="quick-row">
        <span class="qr-label">试试：</span>
        <el-button v-for="s in QUICK" :key="s" size="small" plain @click="q = s; search()">{{ s }}</el-button>
      </div>
    </div>

    <!-- ---------------------------------------------------- 结果 -->
    <div class="panel" v-loading="loading">
      <div class="panel-title">
        检索结果
        <span class="sub" v-if="out">
          {{ out.count }} 条 · 耗时 {{ out.elapsed_ms }}ms · 向量库 {{ out.collection_size }} 块
        </span>
      </div>

      <div v-if="out && !out.hits.length" class="empty-box">
        没有召回到相关片段（宁可不给，也不用不相关的片段凑数）
      </div>

      <div v-for="(h, i) in out?.hits ?? []" :key="i" class="hit">
        <div class="hit-head">
          <span class="hit-idx mono">#{{ i + 1 }}</span>
          <span class="hit-doc">{{ h.doc_title }}</span>
          <el-tag size="small" :type="h.doc_type === 'emergency_plan' ? 'danger' : 'info'">
            {{ h.doc_type === 'emergency_plan' ? '应急预案' : '说明书' }}
          </el-tag>
          <span class="hit-dist mono">距离 {{ h.distance === null ? '—' : h.distance.toFixed(3) }}</span>
        </div>

        <div class="hit-section">{{ h.section }}</div>
        <div class="hit-content">{{ h.content }}</div>

        <div class="hit-foot">
          <span class="hf-src">{{ h.source_file }}</span>
          <span v-for="t in h.device_types" :key="t" class="hf-chip">{{ t }}</span>
        </div>
      </div>

      <div v-if="out?.note" class="kb-note">{{ out.note }}</div>
    </div>

    <!-- ---------------------------------------------------- 文档清单 -->
    <div class="two-col">
      <div class="panel">
        <div class="panel-title">已入库文档</div>
        <el-table :data="docs" size="small" max-height="280">
          <el-table-column prop="doc_title" label="文档" min-width="180" />
          <el-table-column label="类型" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="row.doc_type === 'emergency_plan' ? 'danger' : 'info'">
                {{ row.doc_type === 'emergency_plan' ? '预案' : '说明书' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="chunks" label="块数" width="70" align="center" />
          <el-table-column label="章节" min-width="200">
            <template #default="{ row }">
              <span class="dim">{{ (row.sections || []).join(' / ') || '—' }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="panel">
        <div class="panel-title">知识库状态</div>
        <el-descriptions v-if="status" :column="1" border size="small">
          <el-descriptions-item label="集合">{{ status.collection }}</el-descriptions-item>
          <el-descriptions-item label="向量块">{{ status.chunks }}</el-descriptions-item>
          <el-descriptions-item label="嵌入模型">{{ status.embedding_model }}</el-descriptions-item>
          <el-descriptions-item label="向量目录">{{ status.chroma_dir }}</el-descriptions-item>
          <el-descriptions-item label="源文件">{{ status.raw_files }} 个（{{ status.raw_dir }}）</el-descriptions-item>
          <el-descriptions-item label="已入库">{{ status.indexed_documents }} 份</el-descriptions-item>
        </el-descriptions>
        <div v-if="status?.note" class="kb-note">{{ status.note }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getKnowledgeStatus, listDocuments, searchKnowledge } from '@/api'

const QUICK = [
  '客厅空调滤网多久清洗一次',
  '燃气泄漏了怎么办',
  '门锁电池没电了怎么应急开门',
  '扫地机器人多久清理一次尘盒',
]

const q = ref('')
const k = ref(4)
const docType = ref(null)
const deviceType = ref(null)
const out = ref(null)
const loading = ref(false)

const docs = ref([])
const status = ref(null)

// 设备类型下拉项：直接取已入库文档适用的类型，避免手抄清单过期
const deviceTypes = ref([
  'light', 'ac', 'tv', 'curtain', 'door_lock', 'gas_sensor', 'gas_valve',
  'smoke_sensor', 'water_sensor', 'temp_sensor', 'camera', 'robot_vacuum',
  'air_purifier', 'water_heater', 'breaker',
])

async function search() {
  const text = q.value.trim()
  if (!text) return
  loading.value = true
  try {
    out.value = await searchKnowledge(text, {
      k: k.value,
      doc_type: docType.value || undefined,
      device_type: deviceType.value || undefined,
    })
  } catch (e) {
    ElMessage.error(`检索失败：${e.message}`)
    out.value = null
  } finally {
    loading.value = false
  }
}

async function loadMeta() {
  try {
    docs.value = (await listDocuments()) ?? []
  } catch {
    docs.value = []
  }
  try {
    status.value = await getKnowledgeStatus()
  } catch {
    status.value = null
  }
}

onMounted(loadMeta)
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.search-row {
  display: flex;
  gap: 10px;
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 12px;
}

.fr-label {
  font-size: 12px;
  color: var(--text-sub);
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

.hit {
  margin-bottom: 14px;
  padding: 12px 14px;
  border: 1px solid var(--border-soft);
  border-radius: 9px;
  background: #101a2e;
}

.hit-head {
  display: flex;
  align-items: center;
  gap: 9px;
}

.hit-idx {
  font-size: 12px;
  color: var(--text-dim);
}

.hit-doc {
  font-size: 14px;
  font-weight: 600;
  color: #eaf2ff;
}

.hit-dist {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-dim);
}

.hit-section {
  margin: 6px 0;
  font-size: 12px;
  color: #00e5c0;
}

.hit-content {
  font-size: 13px;
  line-height: 1.8;
  color: var(--text-main);
  white-space: pre-wrap;
}

.hit-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 9px;
  padding-top: 8px;
  border-top: 1px dashed #1e2b45;
}

.hf-src {
  font-size: 11px;
  color: var(--text-dim);
}

.hf-chip {
  padding: 1px 7px;
  border-radius: 10px;
  background: #16223a;
  font-size: 11px;
  color: #8ba1c0;
}

.kb-note {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed #1e2b45;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-dim);
}

.two-col {
  display: grid;
  grid-template-columns: 1.3fr 1fr;
  gap: 16px;
}

.dim {
  color: var(--text-dim);
}

@media (max-width: 1400px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}
</style>
