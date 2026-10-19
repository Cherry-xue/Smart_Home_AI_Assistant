<template>
  <aside class="panel">
    <header class="panel-head">
      <span class="panel-title">器件开关</span>
      <span class="panel-sub tnum">{{ devices.length }} 台可控</span>
    </header>

    <p v-if="controlChannelIssue" class="notice">
      控制通道不可用：{{ controlChannelIssue }}。下方控件暂时只读。
    </p>

    <!-- 每个设备交给公共组件渲染（开关 / 枚举 / 步进 + 回执），
         与「设备」页详情抽屉用的是**同一份实现**，不会两处漂移。 -->
    <DeviceControls
      v-for="d in devices"
      :key="d.id"
      :device="d"
      :capabilities="capsByDevice[d.id] ?? []"
    />
  </aside>
</template>

<script setup>
import DeviceControls from '@/components/DeviceControls.vue'
import { controlChannelIssue } from '@/utils/control'

defineProps({
  /** 只传**可控**设备（writable_count > 0） */
  devices: { type: Array, default: () => [] },
  /** deviceId → capabilities 数组 */
  capsByDevice: { type: Object, default: () => ({}) },
})
</script>

<style scoped>
.panel {
  height: 100%;
  overflow-y: auto;
  padding: 14px 14px 18px;
  border: 1px solid var(--c-line);
}

.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--c-line);
}
.panel-title {
  font-size: 16px;
  font-weight: 500;
}
.panel-sub {
  font-size: var(--fs-small);
  color: var(--c-ink-muted);
}

.notice {
  margin: 12px 0 0;
  padding: 8px;
  font-size: var(--fs-tiny);
  line-height: 1.6;
  color: var(--c-warn);
  border: 1px solid var(--c-warn);
}
</style>
