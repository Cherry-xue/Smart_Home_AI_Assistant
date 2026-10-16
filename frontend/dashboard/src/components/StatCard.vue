<template>
  <div class="stat-card" :class="`tone-${tone}`">
    <div class="sc-head">
      <el-icon v-if="icon"><component :is="icon" /></el-icon>
      <span class="sc-label">{{ label }}</span>
    </div>
    <div class="sc-value mono">
      {{ value }}<span v-if="unit" class="kpi-unit">{{ unit }}</span>
    </div>
    <div v-if="hint" class="sc-hint">{{ hint }}</div>
  </div>
</template>

<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '—' },
  unit: { type: String, default: '' },
  hint: { type: String, default: '' },
  icon: { type: String, default: '' },
  tone: { type: String, default: 'normal' }, // normal / ok / warn / danger
})
</script>

<style scoped>
.stat-card {
  padding: 14px 16px;
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  background: linear-gradient(180deg, #0e1526 0%, #131c30 100%);
}

.sc-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-sub);
  letter-spacing: 1px;
}

.sc-value {
  margin-top: 10px;
  font-size: 27px;
  font-weight: 700;
  color: #fff;
  line-height: 1.1;
}

.sc-hint {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-dim);
}

.tone-ok .sc-value {
  color: #21d07a;
}
.tone-warn .sc-value {
  color: #ffb020;
}
.tone-danger .sc-value {
  color: #ff5c6c;
}
.tone-ok .sc-head {
  color: #21d07a;
}
.tone-warn .sc-head {
  color: #ffb020;
}
.tone-danger .sc-head {
  color: #ff5c6c;
}
</style>
