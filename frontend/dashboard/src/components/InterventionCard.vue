<template>
  <div class="iv-card" :class="{ 'is-conflict': item.kind === 'conflict' }">
    <div class="iv-head">
      <el-icon class="iv-icon" :size="16"><WarningFilled /></el-icon>
      <span class="iv-kind">{{ kindText }}</span>
      <span v-if="item.expires_at" class="iv-expire">{{ expireText }}</span>
    </div>

    <div class="iv-prompt">{{ item.prompt || `请确认 ${item.device_name} 的操作` }}</div>
    <div v-if="item.detail" class="iv-detail">{{ item.detail }}</div>

    <div class="iv-meta">
      <span v-if="item.device_name" class="iv-chip">{{ item.device_name }}</span>
      <span v-if="item.capability" class="iv-chip mono">{{ item.capability }} = {{ item.value }}</span>
      <span v-if="item.question" class="iv-question">来自提问：{{ item.question }}</span>
    </div>

    <!-- 红线提示：批准 ≠ 自动执行，这句话必须出现在每一次确认里 -->
    <div class="iv-redline">
      <el-icon :size="12"><Lock /></el-icon>
      批准只代表"人工已确认并留痕"，高风险设备不会由系统自动执行
    </div>

    <div class="iv-actions">
      <el-button size="small" type="success" :loading="busy" @click="submit('approve')">
        <el-icon><Check /></el-icon> 批准
      </el-button>
      <el-button size="small" type="danger" plain :loading="busy" @click="submit('reject')">
        <el-icon><Close /></el-icon> 驳回
      </el-button>
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
  item: { type: Object, required: true }, // InterventionOut
  index: { type: Number, default: 0 },
})
const emit = defineEmits(['decided'])

const busy = ref(false)
const resultMsg = ref('')

const kindText = computed(() => (props.item.kind === 'conflict' ? '需要你定：目标冲突' : '高风险动作确认'))

const expireText = computed(() => {
  if (!props.item.expires_at) return ''
  return `${fmtDateTime(props.item.expires_at)} 前有效`
})

async function submit(choice) {
  busy.value = true
  resultMsg.value = ''
  try {
    const out = await decidePending(props.item.thread_id, { choice, interventionIndex: props.index })
    resultMsg.value = out?.message ?? '已提交'
    ElMessage({
      type: out?.accepted ? 'success' : 'warning',
      message: out?.accepted ? '已记录你的决定' : (out?.message ?? '该决定未被接受'),
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
  border: 1px solid #4a3520;
  border-radius: 9px;
  background: linear-gradient(180deg, #1d1608 0%, #171c2e 100%);
}

.iv-card.is-conflict {
  border-color: #2c3f63;
  background: linear-gradient(180deg, #101d33 0%, #131c30 100%);
}

.iv-head {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 8px;
}

.iv-icon {
  color: #ffb020;
}

.iv-kind {
  font-size: 13px;
  font-weight: 600;
  color: #ffc862;
}

.iv-expire {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-dim);
}

.iv-prompt {
  font-size: 14px;
  color: #f0e6d2;
  line-height: 1.6;
}

.iv-detail {
  margin-top: 5px;
  font-size: 12px;
  color: var(--text-sub);
  line-height: 1.6;
}

.iv-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 9px;
}

.iv-chip {
  padding: 2px 8px;
  border: 1px solid #3a4a6b;
  border-radius: 4px;
  background: #16223a;
  font-size: 11px;
  color: #c9d6e8;
}

.iv-question {
  font-size: 11px;
  color: var(--text-dim);
}

.iv-redline {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 10px;
  padding: 6px 9px;
  border-radius: 6px;
  background: rgba(255, 92, 108, 0.08);
  font-size: 11px;
  color: #ff8b96;
}

.iv-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}

.iv-result {
  font-size: 12px;
  color: var(--text-sub);
}
</style>
