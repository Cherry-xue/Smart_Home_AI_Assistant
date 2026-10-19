/** 设备控制的共享状态与判定。
 *
 * 为什么要单独一个模块：总览页的右侧「器件开关」和「设备」页的详情抽屉
 * 都要下发指令、都要处理"后端没有控制端点"这种情况。
 * 如果各写一份，错误文案和判定口径就会漂移 —— 这里只写一次。
 */

import { ref } from 'vue'

/** 控制通道不可用的原因（正常恒为 ''）。
 *
 * 后端已经有 `POST /api/devices/{id}/command`，所以正常情况下它永远是空的。
 * 留着是为了**如实反馈**：万一端点被删/改路径（404、405），
 * 界面要变成只读并说明原因，而不是让人反复点一个没反应的开关。 */
export const controlChannelIssue = ref('')

/** 数值型能力的 value_type 取值集合。
 *
 * ⚠️ 后端实际用的是 `int` / `float`，**没有 `number`**。
 * 第一版只判了 `'number'`，导致亮度、目标温度这些全掉进"不支持操作"分支。
 * 保留 `number` 只是防御性兼容。 */
const NUMERIC_TYPES = new Set(['int', 'float', 'number'])

export function isNumericCap(cap) {
  return NUMERIC_TYPES.has(String(cap?.value_type ?? '').toLowerCase())
}

/** 数值能力的步进量：范围大的跳大一点，否则调 0-100 的亮度要点 100 下。 */
export function stepSize(cap) {
  const span = Number(cap?.value_max ?? 100) - Number(cap?.value_min ?? 0)
  return span > 40 ? 5 : 1
}

/** 按步长夹取到能力范围内。 */
export function nextStep(current, cap, dir) {
  const cur = Number(current)
  const base = Number.isFinite(cur) ? cur : Number(cap?.value_min ?? 0)
  const min = Number(cap?.value_min ?? 0)
  const max = Number(cap?.value_max ?? 100)
  return Math.min(max, Math.max(min, base + dir * stepSize(cap)))
}

/** 从回执原文里把正文抠出来，界面只显示人类可读的那一段。 */
export function stripAck(message) {
  if (!message) return ''
  const tail = String(message).split('[回执]').pop() || ''
  return tail.replace(/^设备已确认[:：]?\s*/, '').trim()
}
