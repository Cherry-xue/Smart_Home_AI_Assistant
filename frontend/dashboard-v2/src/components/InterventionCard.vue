<template>
  <!-- 高风险确认 / 目标冲突裁决 共用一张卡片：两者都是"暂停等人决定" -->
  <div class="iv-card" :class="{ 'iv-card--conflict': item.kind === 'conflict' }">
    <div class="iv-head">
      <span class="iv-kind">{{ kindText }}</span>
      <span v-if="item.expires_at" class="iv-expire tnum">{{ expireText }}</span>
    </div>

    <div class="iv-prompt">{{ item.prompt || `请确认 ${item.device_name} 的操作` }}</div>
    <!-- 高风险确认的 detail 恒为同一句模板话术（"只会生成待确认建议…"），读两遍没信息量，
         不展示；目标冲突的 detail 是"为什么冲突"的判据，必须留着 -->
    <div v-if="item.kind === 'conflict' && item.detail" class="iv-detail">{{ item.detail }}</div>

    <div class="iv-meta">
      <span v-if="item.device_name" class="tag">{{ item.device_name }}</span>
      <span v-if="item.capability" class="tag tag--accent">{{ item.capability }} = {{ item.value }}</span>
      <span v-if="item.question" class="iv-question">来自提问：{{ item.question }}</span>
    </div>

    <!-- 红线本身由后端保证（executor 强制改写 + status 默认值），
         批准后的返回消息里也会再说明一次，卡片上不再重复贴一遍 -->

    <div class="iv-actions">
      <!-- 给过决定之后按钮就撤掉：留着人能再点一次，等于给同一件事重复留痕 -->
      <template v-if="!decided">
        <el-button size="small" type="primary" :loading="busy" @click="submit('approve')">批准</el-button>
        <el-button size="small" :loading="busy" @click="submit('reject')">驳回</el-button>
      </template>
      <span
        v-else
        class="iv-done"
        :class="accepted ? `iv-done--${doneChoice}` : 'iv-done--expired'"
      >{{ doneText }}</span>
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

// 给过决定之后不再显示按钮：同一张卡能反复点是"重复留痕"，
// 而且批准是不可撤销的，误触代价太大。
const decided = ref(false)
const doneChoice = ref('')
const accepted = ref(false)
const doneText = computed(() => {
  if (!accepted.value) return '该确认已失效'
  return doneChoice.value === 'approve' ? '已批准' : '已驳回'
})

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
    // 不论采纳与否都收起按钮：已被采纳的不能再点（重复留痕），
    // 已过期的点了也只会再报一次错，留着反而误导。
    decided.value = true
    doneChoice.value = choice
    accepted.value = Boolean(out?.accepted)
    // 采纳时聊天流里会追加一条「已批准 → …」，卡片不必再重复一段长文案
    resultMsg.value = out?.accepted ? '' : out?.message ?? ''
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

/* 已给过决定的标记：占掉按钮原来的位置，卡片高度不跳 */
.iv-done {
  padding: 2px 10px;
  border: 1px solid var(--c-line);
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}
.iv-done--approve {
  border-color: var(--c-good);
  color: var(--c-good);
}
.iv-done--reject,
.iv-done--expired {
  border-color: var(--c-line);
  color: var(--c-ink-muted);
}
</style>
