/** 展示层格式化。
 *
 * 一条硬规矩：**没有值就是 null，不能用 0 冒充**（后端 schema 也是这么定的）。
 * 所以这里的函数在收到 null/undefined 时统一返回 '—'，绝不返回 0。
 */

const DASH = '—'

export function isBlank(v) {
  return v === null || v === undefined || v === ''
}

/** 后端返回的 datetime 是本地时区 naive（没有 Z 后缀），直接当本地时间解析。 */
export function toDate(iso) {
  if (!iso) return null
  // '2026-10-17T19:19:10' → 按本地时区；带 Z 的按 UTC 再转本地
  const s = typeof iso === 'string' ? iso.replace(' ', 'T') : iso
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? null : d
}

const p2 = (n) => String(n).padStart(2, '0')

export function fmtDateTime(iso) {
  const d = toDate(iso)
  if (!d) return DASH
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`
}

export function fmtTime(iso) {
  const d = toDate(iso)
  if (!d) return DASH
  return `${p2(d.getHours())}:${p2(d.getMinutes())}`
}

export function fmtHour(h) {
  return `${p2(h)}:00`
}

/** 相对时间：用于"数据更新于 xx 前"，让人一眼看出数据是不是停了。 */
export function fromNow(iso) {
  const d = toDate(iso)
  if (!d) return DASH
  const sec = Math.floor((Date.now() - d.getTime()) / 1000)
  if (sec < 0) return DASH
  if (sec < 60) return `${sec} 秒前`
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`
  return `${Math.floor(sec / 86400)} 天前`
}

/** 数值格式化；空值返回 '—'，不做 0 填充。 */
export function num(v, digits = 1) {
  if (isBlank(v)) return DASH
  const n = Number(v)
  if (Number.isNaN(n)) return DASH
  return n.toFixed(digits)
}

/** 传感器读数带单位展示：bool 转成"开/关"更好读。 */
export function metricText(v, unit) {
  if (isBlank(v)) return DASH
  if (typeof v === 'boolean') return v ? '开' : '关'
  if (typeof v === 'number') {
    const s = Number.isInteger(v) ? String(v) : v.toFixed(1)
    return unit ? `${s}${unit}` : s
  }
  return String(v)
}

// ---------------------------------------------------------------- 语义映射
export const QUALITY_META = {
  good: { label: '正常', type: 'success' },
  suspect: { label: '可疑', type: 'warning' },
  bad: { label: '异常', type: 'danger' },
}

export const PERIOD_META = {
  peak: { label: '峰电', color: '#ff5c6c' },
  flat: { label: '平电', color: '#ffb020' },
  valley: { label: '谷电', color: '#21d07a' },
}

export function periodLabel(p) {
  return PERIOD_META[p]?.label ?? p ?? '未分时'
}

export const STATUS_META = {
  ok: { label: '正常', type: 'success' },
  warn: { label: '提示', type: 'warning' },
  clamped: { label: '已夹取', type: 'warning' },
  rejected: { label: '已剔除', type: 'info' },
  duplicate: { label: '被覆盖', type: 'info' },
  pending_confirm: { label: '待确认', type: 'warning' },
  executed: { label: '已下发', type: 'success' },
  failed: { label: '失败', type: 'danger' },
}

export function statusMeta(s) {
  return STATUS_META[s] ?? { label: s || '未知', type: 'info' }
}

/** 设备类型 → 中文名（后端只给英文 type，界面上要看得懂）。 */
export const DEVICE_TYPE_LABEL = {
  light: '灯具',
  light_strip: '灯带',
  curtain: '窗帘',
  ac: '空调',
  tv: '电视',
  speaker: '音响',
  socket: '智能插座',
  door_lock: '门锁',
  gas_valve: '燃气阀',
  breaker: '电气总闸',
  temp_sensor: '温湿度传感器',
  humidity_sensor: '湿度传感器',
  smoke_sensor: '烟雾报警器',
  gas_sensor: '燃气探测器',
  water_sensor: '水浸传感器',
  pir_sensor: '人体存在传感器',
  door_sensor: '门窗传感器',
  camera: '摄像头',
  air_purifier: '空气净化器',
  fresh_air: '新风系统',
  floor_heating: '地暖',
  water_heater: '热水器',
  robot_vacuum: '扫地机器人',
  washing_machine: '洗衣机',
  fridge: '冰箱',
  meter: '电表',
}

export function typeLabel(t) {
  return DEVICE_TYPE_LABEL[t] ?? t ?? '设备'
}
