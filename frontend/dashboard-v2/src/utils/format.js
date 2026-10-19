/** 展示层格式化。
 *
 * 一条硬规矩：**没有值就是 null，不能用 0 冒充**（后端 schema 也是这么定的）。
 * 所以这里所有函数收到 null/undefined 时统一返回 '—'，绝不返回 0。
 *
 * ⚠️ 这一层是从旧大屏迁移过来的，约定保持一致（空值 '—'、布尔转"开/关"、
 *    naive datetime 当本地时间解析）。新增了"指标英文码 → 中文名"的映射，
 *    因为后端 metric_code 是英文，直接显示在界面上看不懂。
 */

const DASH = '—'

export function isBlank(v) {
  return v === null || v === undefined || v === ''
}

/** 后端返回的 datetime 是本地时区 naive（没有 Z 后缀），直接当本地时间解析。 */
export function toDate(iso) {
  if (!iso) return null
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

/**
 * 相对时间："数据更新于 xx 前"。
 *
 * ⚠️ 基准时间**必须传数据锚点**，不能默认用 `Date.now()`。
 *    原因：模拟器按倍速跑（`--max-ahead 3`），数据时间可能领先真实时钟几分钟，
 *    拿真实时钟去减会得到负数 → 恒显示"—"（这个坑实测踩过：
 *    房间面板的"数据更新"永远显示"—"）。不传就退回 `Date.now()`。
 */
export function fromNow(iso, anchorIso = null) {
  const d = toDate(iso)
  if (!d) return DASH
  const anchor = anchorIso ? toDate(anchorIso) : null
  const base = anchor ? anchor.getTime() : Date.now()
  const sec = Math.floor((base - d.getTime()) / 1000)
  if (sec < 0) return DASH
  if (sec < 60) return `${sec} 秒前`
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`
  return `${Math.floor(sec / 86400)} 天前`
}

export function num(v, digits = 1) {
  if (isBlank(v)) return DASH
  const n = Number(v)
  if (Number.isNaN(n)) return DASH
  return n.toFixed(digits)
}

// ---------------------------------------------------------------- 指标语义
/** metric_code（后端英文）→ 中文名。界面上必须给用户看得懂的字。 */
export const METRIC_LABEL = {
  temperature: '温度',
  humidity: '湿度',
  pm25: 'PM2.5',
  co2: 'CO₂',
  tvoc: 'TVOC',
  power_on: '开关',
  brightness: '亮度',
  color_temp: '色温',
  position: '开合度',
  mode: '模式',
  target_temp: '目标温度',
  fan_speed: '风速',
  lock_state: '门锁',
  power_state: '总闸',
  valve_state: '阀门',
  gas_concentration: '燃气浓度',
  smoke_detected: '烟雾',
  water_detected: '水浸',
  presence: '有人',
  activity_level: '活动度',
  illuminance: '照度',
  power_w: '功率',
  energy_kwh: '电量',
  volume: '音量',
}

export function metricLabel(code) {
  return METRIC_LABEL[code] ?? code ?? DASH
}

/** 枚举型读数的取值 → 中文。 */
const ENUM_TEXT = {
  lock_state: { locked: '已锁', unlocked: '未锁', jammed: '卡住' },
  power_state: { on: '合闸', off: '断电', tripped: '跳闸' },
  valve_state: { open: '已开', closed: '已关', auto: '自动' },
  position: { open: '全开', closed: '全关', half: '半开' },
  // 空调：原始值是英文，界面上直接显示 cool/fan/auto 很别扭
  mode: { cool: '制冷', heat: '制热', fan: '送风', dry: '除湿', auto: '自动' },
  fan_speed: { auto: '自动', low: '低风', mid: '中风', high: '高风', silent: '静音' },
}

/**
 * 读数 → 显示文本。
 *
 * ⚠️ 布尔值的语义**不是统一的**：`power_on=true` 是"开"，
 * 但 `smoke_detected=true` 是"报警"、`water_detected=true` 是"漏水"。
 * 所以必须按 metric_code 区分，不能一律 `true ? '开' : '关'`。
 */
export function metricText(code, value, unit) {
  if (isBlank(value)) return DASH

  if (code === 'smoke_detected') return value ? '报警' : '正常'
  if (code === 'water_detected') return value ? '漏水' : '正常'
  if (code === 'presence') return value ? '有人' : '无人'

  if (typeof value === 'boolean') return value ? '开' : '关'

  const enumMap = ENUM_TEXT[code]
  if (enumMap && typeof value === 'string' && enumMap[value]) return enumMap[value]

  if (typeof value === 'number') {
    const s = Number.isInteger(value) ? String(value) : value.toFixed(1)
    return unit ? `${s} ${unit}` : s
  }
  return String(value)
}

/** 质量标记 → 中文 + 语义色类名。 */
export const QUALITY_META = {
  good: { label: '正常', cls: 'tag--good' },
  suspect: { label: '可疑', cls: 'tag--warn' },
  bad: { label: '异常', cls: 'tag--danger' },
}

export function qualityMeta(q) {
  return QUALITY_META[q] ?? { label: q || '未知', cls: '' }
}

export const PERIOD_META = {
  peak: { label: '峰电' },
  flat: { label: '平电' },
  valley: { label: '谷电' },
}

export function periodLabel(p) {
  return PERIOD_META[p]?.label ?? p ?? '未分时'
}

export const STATUS_META = {
  ok: { label: '正常', cls: 'tag--good' },
  warn: { label: '提示', cls: 'tag--warn' },
  clamped: { label: '已夹取', cls: 'tag--warn' },
  rejected: { label: '已剔除', cls: '' },
  duplicate: { label: '被覆盖', cls: '' },
  pending_confirm: { label: '待确认', cls: 'tag--warn' },
  executed: { label: '已下发', cls: 'tag--good' },
  failed: { label: '失败', cls: 'tag--danger' },
}

export function statusMeta(s) {
  return STATUS_META[s] ?? { label: s || '未知', cls: '' }
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
  main_breaker: '电气总闸',
  breaker: '电气总闸',
  temp_sensor: '温湿度传感器',
  humidity_sensor: '湿度传感器',
  air_sensor: '空气质量传感器',
  smoke_sensor: '烟雾报警器',
  gas_sensor: '燃气探测器',
  water_leak_sensor: '水浸传感器',
  water_sensor: '水浸传感器',
  pir_sensor: '人体存在传感器',
  mmwave_radar: '毫米波雷达',
  door_sensor: '门窗传感器',
  camera: '摄像头',
  air_purifier: '空气净化器',
  fresh_air: '新风系统',
  floor_heating: '地暖',
  water_heater: '热水器',
  robot_vacuum: '扫地机器人',
  washing_machine: '洗衣机',
  fridge: '冰箱',
  humidifier: '加湿器',
  meter: '电表',
}

export function typeLabel(t) {
  return DEVICE_TYPE_LABEL[t] ?? t ?? '设备'
}

// ---------------------------------------------------------------- 数据来源
/**
 * 数据来源角标文案。
 *
 * ⚠️ 项目红线：**模拟数据必须显式标注来源**，不得伪装成真实实测值。
 * 当前本地环境里 MQTT broker 是模拟器在喂数据，所以 `mqtt` 也标成"模拟数据"。
 * 将来真接了硬件网关（`real_gateway`），这里会自动变成"真实网关"；
 * **但如果有人用真设备喂 MQTT，必须把 `mqtt` 这条改掉**，否则就是撒谎。
 *
 * ⚠️ 只在这里定义一次 —— 顶栏和总览页都用它，避免两处文案不一致。
 */
export const DATA_MODE_LABEL = {
  mock: '模拟数据',
  mqtt: '模拟数据 · MQTT',
  csv: '回放数据 · CSV',
  real_gateway: '真实网关',
}

export function dataModeLabel(mode) {
  return DATA_MODE_LABEL[mode] ?? (mode ? `数据来源 ${mode}` : '数据来源未知')
}

// ---------------------------------------------------------------- 房间几何辅助
/** 从 key_metrics 里挑一个"最能代表这间房"的读数，用于房间里那行小字。 */
const PRIORITY = [
  'temperature',
  'humidity',
  'pm25',
  'co2',
  'gas_concentration',
  'lock_state',
  'power_state',
  'brightness',
  'power_w',
  'position',
  'presence',
  'power_on',
]

export function pickKeyMetrics(metrics = [], max = 2) {
  if (!metrics?.length) return []
  const sorted = [...metrics].sort((a, b) => {
    const ia = PRIORITY.indexOf(a?.metric_code)
    const ib = PRIORITY.indexOf(b?.metric_code)
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
  })
  return sorted.slice(0, max)
}

/** 把一条 key_metric 渲染成"温度 24.1 ℃"这样的一行。 */
export function keyMetricLine(km) {
  if (!km) return DASH
  const label = metricLabel(km.metric_code)
  const text = metricText(km.metric_code, km.value, km.unit)
  return `${label} ${text}`
}
