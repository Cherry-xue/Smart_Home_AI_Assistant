<template>
  <div ref="el" :style="{ height: typeof height === 'number' ? `${height}px` : height }" />
</template>

<script setup>
/** ECharts 封装。
 *
 * 只做三件必要的事：初始化、随容器尺寸 resize、卸载时 dispose。
 * 不做响应式 props 深度 diff —— 调用方直接换整个 option 更简单也更可控。
 *
 * ⚠️ 图表里的颜色都是**调用方显式传进来**的（canvas 里读不到 CSS 变量）。
 *    取色统一走 `utils/theme.js` 的 `cssVar()`，这样配色仍然只在
 *    `styles/tokens.css` 一处定义。
 */
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  option: { type: Object, default: null },
  height: { type: [Number, String], default: 260 },
})

const el = ref(null)
const chart = shallowRef(null)
let ro = null

function render() {
  if (!chart.value || !props.option) return
  chart.value.setOption(props.option, true) // notMerge=true：切换数据时不会残留上一份 series
}

onMounted(() => {
  chart.value = echarts.init(el.value, null, { renderer: 'canvas' })
  render()
  ro = new ResizeObserver(() => chart.value?.resize())
  ro.observe(el.value)
})

watch(() => props.option, render, { deep: false })

onBeforeUnmount(() => {
  ro?.disconnect()
  chart.value?.dispose()
  chart.value = null
})

defineExpose({
  getChart: () => chart.value,
})
</script>
