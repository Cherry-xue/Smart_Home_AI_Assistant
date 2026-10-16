<template>
  <div class="sections">
    <!-- 参与的专业助手 -->
    <div v-if="(sections.by_agent || []).length" class="sec">
      <div class="sec-title">参与的专业助手</div>
      <div class="agent-chips">
        <span v-for="a in sections.by_agent" :key="a.agent" class="agent-chip">
          {{ a.label }}
          <span v-if="a.action_count" class="chip-num">· {{ a.action_count }} 个动作</span>
        </span>
      </div>
    </div>

    <!-- 观测事实 -->
    <div v-if="(sections.observed_facts || []).length" class="sec">
      <div class="sec-title">观测事实</div>
      <ul class="bullets">
        <li v-for="(f, i) in sections.observed_facts" :key="i">{{ renderFact(f) }}</li>
      </ul>
    </div>

    <!-- 分析 -->
    <div v-if="(sections.analyses || []).length" class="sec">
      <div class="sec-title">分析</div>
      <ul class="bullets">
        <li v-for="(a, i) in sections.analyses" :key="i">{{ a }}</li>
      </ul>
    </div>

    <!-- 各助手建议 -->
    <div v-if="(sections.suggestions || []).length" class="sec">
      <div class="sec-title">助手结论</div>
      <ul class="bullets">
        <li v-for="(s, i) in sections.suggestions" :key="i">{{ s }}</li>
      </ul>
    </div>

    <!-- 被剔除的动作（能复现原因，这是编排层的卖点） -->
    <div v-if="(sections.rejected || []).length" class="sec">
      <div class="sec-title">被剔除的动作</div>
      <ul class="bullets dim-list">
        <li v-for="(r, i) in sections.rejected" :key="i">
          {{ r.device_name }} · {{ r.capability }}={{ r.value }} —— {{ r.reason }}
        </li>
      </ul>
    </div>

    <!-- 编排说明 -->
    <div v-if="(sections.plan_notes || []).length" class="sec">
      <div class="sec-title">编排说明</div>
      <ul class="bullets dim-list">
        <li v-for="(n, i) in sections.plan_notes" :key="i">{{ n }}</li>
      </ul>
    </div>

    <!-- 待确认 -->
    <div v-if="(sections.pending || []).length" class="sec">
      <div class="sec-title warn">待人工确认</div>
      <ul class="bullets warn-list">
        <li v-for="(p, i) in sections.pending" :key="i">{{ typeof p === 'string' ? p : JSON.stringify(p) }}</li>
      </ul>
    </div>

    <!-- 不确定项 -->
    <div v-if="(sections.uncertainties || []).length" class="sec">
      <div class="sec-title">数据不足 / 不确定</div>
      <ul class="bullets dim-list">
        <li v-for="(u, i) in sections.uncertainties" :key="i">{{ u }}</li>
      </ul>
    </div>

    <!-- 引用来源：不给来源的引用不能算引用 -->
    <div v-if="(sections.citations || []).length" class="sec">
      <div class="sec-title">引用来源</div>
      <ul class="bullets dim-list">
        <li v-for="(c, i) in sections.citations" :key="i">
          {{ c.source_file }} · {{ c.section }}
        </li>
      </ul>
    </div>

    <!-- 参考记忆 -->
    <div v-if="(sections.memory_refs || []).length" class="sec">
      <div class="sec-title">参考长期记忆</div>
      <ul class="bullets dim-list">
        <li v-for="(m, i) in sections.memory_refs" :key="i">{{ m.content ?? m }}</li>
      </ul>
    </div>
  </div>
</template>

<script setup>
defineProps({
  sections: { type: Object, default: () => ({}) },
})

/** observed_facts 里既可能是渲染好的字符串，也可能是 dict（都兼容一下） */
function renderFact(f) {
  if (typeof f === 'string') return f
  if (f && typeof f === 'object') {
    return f.text ?? f.render ?? f.summary ?? JSON.stringify(f)
  }
  return String(f)
}
</script>

<style scoped>
.sec {
  margin-bottom: 12px;
}

.sec-title {
  margin-bottom: 5px;
  font-size: 12px;
  font-weight: 600;
  color: #9fb6d6;
  letter-spacing: 1px;
}

.sec-title.warn {
  color: #ffc862;
}

.bullets {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.85;
  color: var(--text-main);
}

.dim-list {
  color: var(--text-sub);
}

.warn-list {
  color: #ffc862;
}

.agent-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.agent-chip {
  padding: 3px 9px;
  border: 1px solid #2b3d5e;
  border-radius: 12px;
  background: #16223a;
  font-size: 12px;
  color: #c9d6e8;
}

.chip-num {
  color: var(--text-dim);
}
</style>
