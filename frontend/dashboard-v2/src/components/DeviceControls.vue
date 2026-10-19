<template>
  <div class="dev" :class="{ 'dev--flat': !showHeader }">
    <div v-if="showHeader" class="dev-head">
      <span class="dev-name">{{ device.name }}</span>
      <span class="tag">{{ typeLabel(device.device_type) }}</span>
    </div>

    <p v-if="device.high_risk" class="dev-risk">高风险设备 - 需要手动确认</p>
    <p v-else-if="!device.online" class="dev-offline">设备离线，指令可能不会生效</p>

    <div v-for="cap in writableCaps" :key="cap.code" class="cap">
      <div class="cap-label">{{ cap.display_name || metricLabel(cap.code) }}</div>

      <!-- 开关型 -->
      <button
        v-if="cap.value_type === 'bool'"
        class="sw"
        :class="{ 'sw--on': currentValue(cap.code) === true }"
        :disabled="busyKey === cap.code || !!controlChannelIssue"
        :title="currentValue(cap.code) === true ? '点击关闭' : '点击开启'"
        @click="apply(cap, currentValue(cap.code) !== true)"
      >
        <span class="sw-track"><span class="sw-knob" /></span>
        <span class="sw-text">{{ currentValue(cap.code) === true ? '开' : '关' }}</span>
      </button>

      <!-- 枚举型：下拉。以前是分段按钮，选项一多（模式 5 个、风速 4 个）
           会把面板挤出一排排小方块，既占地方又不整齐 -->
      <el-select
        v-else-if="cap.enum_values?.length"
        :model-value="currentValue(cap.code)"
        size="small"
        class="cap-select"
        placeholder="未设置"
        :disabled="busyKey === cap.code || !!controlChannelIssue"
        @update:model-value="(v) => apply(cap, v)"
      >
        <el-option
          v-for="opt in cap.enum_values"
          :key="String(opt)"
          :label="metricText(cap.code, opt, null)"
          :value="opt"
        />
      </el-select>

      <!-- 数值型：⚠️ value_type 是 int / float，不是 number -->
      <div v-else-if="isNumericCap(cap)" class="num">
        <!-- ⚠️ 拖动过程中只更新**本地**值，松手（change）才下发一条指令。
             以前是"加减按钮点一下发一条"：把亮度从 20 调到 80 要发 12 条、
             等 12 次设备回执，慢到没法用。现在一次定位、一次下发。 -->
        <el-slider
          class="num-slider"
          :model-value="numValue(cap)"
          :min="numRange(cap).min"
          :max="numRange(cap).max"
          :step="numRange(cap).step"
          :disabled="busyKey === cap.code || !!controlChannelIssue"
          @input="(v) => setWish(cap, v)"
          @change="(v) => apply(cap, v)"
        />
        <!-- 可以直接敲一个数：回车 / 失焦下发一次。
             ⚠️ 刻意不显示加减箭头（`controls="false"`）—— 箭头点一下就是一条指令，
                又变回"一点点调、每一下都等回报"。 -->
        <el-input-number
          class="num-input"
          :model-value="numValue(cap)"
          :min="numRange(cap).min"
          :max="numRange(cap).max"
          :step="numRange(cap).step"
          :controls="false"
          size="small"
          :disabled="busyKey === cap.code || !!controlChannelIssue"
          @update:model-value="(v) => setWish(cap, v)"
          @change="(v) => commitInput(cap, v)"
        />
        <span v-if="cap.unit" class="num-unit">{{ cap.unit }}</span>
      </div>

      <span v-else class="empty">暂不支持在此操作</span>
    </div>

    <p v-if="!writableCaps.length" class="empty">这台设备没有可写能力。</p>

    <!-- 最近一条指令 + 设备回执：把"点了到底成没成"如实摆出来 -->
    <p v-if="lastCmd" class="dev-last" :class="`dev-last--${lastCmd.tone}`">
      {{ lastCmd.text }}
    </p>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { listDeviceCommands, sendDeviceCommand } from '@/api'
import { isNumericCap, controlChannelIssue, stepSize, stripAck } from '@/utils/control'
import { metricLabel, metricText, typeLabel } from '@/utils/format'

const props = defineProps({
  /** 设备项（来自 /api/devices 的 items） */
  device: { type: Object, required: true },
  /** 该设备的能力档案（来自 /api/devices/{id}/capabilities） */
  capabilities: { type: Array, default: () => [] },
  /** 是否显示"设备名 + 类型"那一行。在总览页每个设备一组，显示；单设备详情页可关掉 */
  showHeader: { type: Boolean, default: true },
})

const busyKey = ref('')
/** 最近一条指令的状态：{ tone: ok|warn|bad|wait, text } */
const lastCmd = ref(null)

/** 设备关闭后就**没有意义**的能力：灯都关了还摆一个"亮度"滑块是自相矛盾的。 */
const OFF_HIDES_CAP = { light: ['brightness', 'color_temp'] }

/**
 * 关灯后隐藏亮度。
 *
 * ⚠️ 只有设备**明确上报过关闭**才隐藏：刚上线的设备缓存里可能还没有 `power_on`，
 *    这时不能猜，否则控制面板会莫名少一行。
 */
function capHidden(cap) {
  const list = OFF_HIDES_CAP[props.device?.device_type]
  if (!list?.includes(cap.code)) return false
  return currentValue('power_on') === false
}

const writableCaps = computed(() =>
  props.capabilities.filter((c) => c.writable && !capHidden(c)),
)

function currentValue(code) {
  return props.device?.latest?.[code]?.value ?? null
}

// ---- 数值型：本地"期望值" ------------------------------------------------
// ⚠️ 为什么需要：项目红线是"只有真改了值才能说改了值"，所以界面上的设备值
//    必须等回执。但滑块/输入框要**立刻反映用户自己拨到哪**，否则松手后滑块
//    会弹回原值，看起来像没生效。于是单独存一份"我期望的值"，
//    等设备回执到了（或确认失败）就删掉、换回设备真实值。
const wish = reactive({})

function numRange(cap) {
  const min = Number(cap?.value_min ?? 0)
  const max = Number(cap?.value_max ?? 100)
  return { min, max, step: Number(cap?.value_step) || stepSize(cap) }
}

function numValue(cap) {
  const w = wish[cap.code]
  if (w != null) return Number(w)
  const v = Number(currentValue(cap.code))
  return Number.isFinite(v) ? v : numRange(cap).min
}

/** 拖动/输入过程中只记本地值，**不下发**。 */
function setWish(cap, v) {
  wish[cap.code] = v
}

/** 输入框里的数可能被清空（Element 会给 `null`）→ 不下发一条空指令，
 *  把框推回原来的值就行。 */
function commitInput(cap, v) {
  if (v == null || !Number.isFinite(Number(v))) {
    setWish(cap, numValue(cap))
    return
  }
  apply(cap, v)
}

/**
 * 下发后**轮询回执**（不是只查一次）。
 *
 * ⚠️ 以前只等 1.6 秒查一次：设备的 cmd_ack 晚一点到，界面就永远停在
 * "已下发，等待设备回报…"，用户会以为指令没生效 —— 这正是"发了空调指令没反应"的观感来源。
 * 现在最多轮询 ~12 秒，超时也要给一句明确的结论。
 */
async function refreshLast({ tries = 8, delayMs = 1500 } = {}) {
  for (let i = 0; i < tries; i++) {
    await new Promise((r) => setTimeout(r, delayMs))
    try {
      const out = await listDeviceCommands(props.device.id, { limit: 1 })
      const rec = out?.items?.[0]
      if (!rec) continue

      if (rec.acknowledged) {
        lastCmd.value = { tone: 'ok', text: `设备已回报：${stripAck(rec.result_message)}` }
        return
      }
      if (rec.status === 'pending_confirm') {
        lastCmd.value = { tone: 'warn', text: '已生成待确认项，等待人工确认' }
        return
      }
      if (rec.status === 'failed') {
        lastCmd.value = { tone: 'bad', text: `下发失败：${rec.result_message || '未知原因'}` }
        return
      }
      lastCmd.value = { tone: 'wait', text: '已下发，等待设备回报…' }
    } catch {
      /* 查询失败不改结论，继续轮询 */
    }
  }
  // 超时也要说清楚：多半是模拟器/设备没在跑（没人消费 cmd 主题）
  lastCmd.value = {
    tone: 'bad',
    text: '已下发，但 12 秒内没收到设备回执 —— 检查模拟器 / MQTT 是否在运行',
  }
}

async function apply(cap, value) {
  busyKey.value = cap.code
  // 滑块松手 / 输入框回车才走到这里 → 立刻把用户拨到的位置显示出来
  wish[cap.code] = value
  let sent = false
  try {
    const out = await sendDeviceCommand(props.device.id, { capability: cap.code, value })
    // ⚠️ **不做乐观更新**：只有设备回报之后，界面上的值才会通过轮询改变。
    //    措辞也必须是"已下发"而不是"已开启"（只有真改了值才能说改了值）。
    const tone = out.status === 'pending_confirm' ? 'warn' : out.status === 'failed' ? 'bad' : 'wait'
    lastCmd.value = {
      tone,
      text:
        out.status === 'pending_confirm'
          ? '已生成待确认项，请到「消息」里手动确认'
          : out.status === 'failed'
            ? out.message
            : `已下发 ${out.capability_label} = ${metricText(cap.code, value, cap.unit)}，等待设备回报…`,
    }
    ElMessage({
      message: out.message,
      type: out.status === 'pending_confirm' ? 'warning' : out.status === 'failed' ? 'error' : 'success',
      duration: out.status === 'executed' ? 2600 : 4200,
    })
    if (out.status === 'executed') {
      sent = true
      // 回执到了（或等满 12 秒确认没回执）就丢掉期望值，换回设备真实上报的值 ——
      // "我期望 80%"和"设备确实是 80%"是两件事，不能一直混着显示。
      refreshLast().finally(() => {
        delete wish[cap.code]
      })
    }
  } catch (e) {
    // ⚠️ 万一后端控制端点不在（路径被改/被删），置一个全局标记，
    //    让上层把整块面板变成只读并说明原因 —— 反复弹同一个错只会让人以为是偶发故障。
    if (e.status === 404 || e.status === 405) {
      controlChannelIssue.value = `后端返回 ${e.status}，控制端点可能不存在`
    }
    ElMessage({ message: `下发失败：${e.message}`, type: 'error', duration: 4500 })
  } finally {
    busyKey.value = ''
    // 没发出去 / 被拦下了（高风险待确认、失败）→ 不占着期望值，界面回到设备真实值
    if (!sent) delete wish[cap.code]
  }
}
</script>

<style scoped>
.dev {
  padding: 12px 0;
  border-bottom: 1px solid var(--c-line-soft);
}
.dev:last-child {
  border-bottom: none;
}
/* 单设备详情里用：不再需要分隔线和内边距 */
.dev--flat {
  padding: 0;
  border-bottom: none;
}

.dev-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.dev-name {
  font-size: var(--fs-body);
}
.dev-risk {
  margin: 6px 0 0;
  font-size: var(--fs-tiny);
  color: var(--c-high-risk);
}
.dev-offline {
  margin: 6px 0 0;
  font-size: var(--fs-tiny);
  color: var(--c-warn);
}
.dev-last {
  margin: 8px 0 0;
  font-size: var(--fs-tiny);
  line-height: 1.5;
}
.dev-last--ok {
  color: var(--c-good);
}
.dev-last--warn {
  color: var(--c-warn);
}
.dev-last--bad {
  color: var(--c-danger);
}
.dev-last--wait {
  color: var(--c-ink-muted);
}

.cap {
  margin-top: 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.cap:first-of-type {
  margin-top: 8px;
}
.cap-label {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
}

/* ---- 开关 ---- */
.sw {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.sw:disabled {
  opacity: 0.45;
  cursor: default;
}
.sw-track {
  position: relative;
  display: inline-block;
  width: 34px;
  height: 18px;
  border: 1px solid var(--c-line);
  background: var(--c-surface-soft);
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.sw-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  background: var(--c-ink-muted);
  transition: transform var(--dur) var(--ease), background var(--dur) var(--ease);
}
.sw--on .sw-track {
  border-color: var(--c-accent);
  background: var(--c-accent-soft);
}
.sw--on .sw-knob {
  transform: translateX(16px);
  background: var(--c-accent);
}
.sw-text {
  font-size: var(--fs-small);
  color: var(--c-ink-soft);
  min-width: 1em;
}

/* ---- 枚举下拉 ---- */
.cap-select {
  width: 160px;
  flex: 0 0 auto;
}
/* ---- 数值：滑杆 + 可直接输入 ---- */
.num {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.num-slider {
  flex: 1;
  min-width: 0;
  /* Element 的滑杆默认带 16px 上下留白，一屏放好几台设备时会撑得很散 */
  margin: 0;
  height: 20px;
}
.num-input {
  flex: 0 0 64px;
  width: 64px;
}
.num-unit {
  flex: none;
  font-size: var(--fs-small);
  color: var(--c-ink-muted);
}
</style>
