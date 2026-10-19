<template>
  <!-- 高风险确认 / 目标冲突裁决 共用一张卡片：两者都是"暂停等人决定" -->
  <div class="iv-card" :class="{ 'iv-card--conflict': item.kind === 'conflict' }">
    <div class="iv-head">
      <span class="iv-kind">{{ kindText }}</span>
      <span v-if="item.expires_at" class="iv-expire tnum">{{ expireText }}</span>
    </div>

    <div class="iv-prompt">{{ item.prompt || `请确认 ${item.device_name} 的操作` }}</div>
    <div v-if="item.detail" class="iv-detail">{{ item.detail }}</div>

    <div class="iv-meta">
      <span v-if="item.device_name" class="tag">{{ item.device_name }}</span>
      <span v-if="item.capability" class="tag tag--accent">{{ item.capability }} = {{ item.value }}</span>
      <span v-if="item.question" class="iv-question">来自提问：{{ item.question }}</span>
    </div>

    <!-- 红线：批准 ≠ 自动执行。这句话必须出现在每一次确认里 -->
    <div class="iv-redline">批准只表示"人已确认并留痕"，高风险设备不会由系统自动执行</div>

    <div class="iv-actions">
      <el-button size="small" type="primary" :loading="busy" @click="submit('approve')">批准</el-button>
      <el-button size="small" :loading="busy" @click="submit('reject')">驳回</el-button>
      <span v-if="resultMsg" class="iv-result">{{ resultMsg }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { decidePending } from '@/api'
import { fmtDateTime } from '@/utils/format'

const props = defineProps({
  /** InterventionOut：后端 schemas/confirmation.py */
  item: { type: Object, required: true },
  /** 同一轮里可能同时挂着多个待确认，提交时要指名是第几条 */
  index: { type: Number, default: 0 },
})
const emit = defineEmits(['decided'])

const busy = ref(false)
const resultMsg = ref('')

const kindText = computed(() =>
  props.item.kind === 'conflict' ? '需要你定：目标冲突' : '高风险动作确认',
)

const expireText = computed(() =>
  props.item.expires_at ? `${fmtDateTime(props.item.expires_at)} 前有效` : '',
)

async function submit(choice) {
  busy.value = true
  resultMsg.value = ''
  try {
    const out = await decidePending(props.item.thread_id, {
      choice,
      interventionIndex: props.index,
    })
    resultMsg.value = out?.message ?? '已提交'
    ElMessage({
      type: out?.accepted ? 'success' : 'warning',
      message: out?.accepted ? '已记录你的决定' : out?.message ?? '该决定未被接受',
    })
    emit('decided', { choice, out })
  } catch (e) {
    ElMessage.error(`提交失败：${e.message}`)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.iv-card {
  margin: 10px 0;
  padding: 12px 14px;
  border: 1px solid var(--c-high-risk);
  background: #fdf6ec;
}

.iv-card--conflict {
  border-color: var(--c-accent);
  background: var(--c-accent-soft);
}

.iv-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.iv-kind {
  font-size: var(--fs-body);
  font-weight: 600;
  color: var(--c-high-risk);
}

.iv-card--conflict .iv-kind {
  color: var(--c-accent);
}

.iv-expire {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}

.iv-prompt {
  font-size: var(--fs-body);
  line-height: 1.7;
  color: var(--c-ink);
}

.iv-detail {
  margin-top: 4px;
  font-size: var(--fs-small);
  line-height: 1.7;
  color: var(--c-ink-soft);
}

.iv-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 9px;
}

.iv-question {
  font-size: var(--fs-tiny);
  color: var(--c-ink-muted);
}

.iv-redline {
  margin-top: 10px;
  padding: 6px 9px;
  border: 1px solid var(--c-danger);
  font-size: var(--fs-tiny);
  color: var(--c-danger);
}

.iv-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}

.iv-result {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
</style>
