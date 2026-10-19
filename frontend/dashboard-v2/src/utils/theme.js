/** 读取 CSS 变量的实际值。
 *
 * 为什么需要它：项目约定"颜色只在 `styles/tokens.css` 定义"，
 * 但 **ECharts 画在 canvas 里，读不到 CSS 变量**，只能拿到具体色值。
 * 这个函数把令牌读出来再传给 ECharts，约定就还是成立的 ——
 * 换配色仍然只改 tokens.css 一个文件。
 *
 * ⚠️ 只在组件 setup 阶段（DOM 就绪后）调用；SSR 环境下返回 fallback。
 */
export function cssVar(name, fallback = '') {
  if (typeof window === 'undefined' || !document?.documentElement) return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(name)
  return v && v.trim() ? v.trim() : fallback
}

/** 一次性取一组令牌，避免每处都写一遍名字。 */
export function chartPalette() {
  return {
    line: cssVar('--c-chart-line', '#1f6feb'),
    areaTop: cssVar('--c-chart-area-top', 'rgba(31,111,235,0.2)'),
    areaBottom: cssVar('--c-chart-area-bottom', 'rgba(31,111,235,0)'),
    axis: cssVar('--c-chart-axis', '#d5d8dd'),
    label: cssVar('--c-chart-label', '#7a828c'),
    grid: cssVar('--c-chart-grid', '#f0f1f3'),
    bad: cssVar('--c-chart-bad', '#c62828'),
    tipBg: cssVar('--c-chart-tip-bg', '#ffffff'),
    tipBorder: cssVar('--c-chart-tip-border', '#e3e5e8'),
  }
}
