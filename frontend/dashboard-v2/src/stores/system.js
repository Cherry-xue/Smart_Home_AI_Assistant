/** 全局系统状态：健康信息 + 待确认事项。
 *
 * 为什么要单独一个 store：顶栏的"消息"红点、左下角的"数据时间"、底部的"在线设备"
 * 都要用同一份数据；如果每个组件各查一次，轮询次数会翻几倍。
 */

import { defineStore } from 'pinia'
import { getHealth, listPending } from '@/api'

export const useSystemStore = defineStore('system', {
  state: () => ({
    health: null,
    healthError: null,
    pending: [],
    pendingError: null,
    loading: false,
    lastHealthAt: null,
    _healthTimer: null,
    _pendingTimer: null,
  }),

  getters: {
    mqttConnected: (s) => s.health?.mqtt?.connected === true,
    /** 设备汇总 —— ⚠️ 来自 /api/health，键是 total/rooms/high_risk/online */
    deviceStats: (s) => s.health?.devices ?? {},
    /**
     * 数据锚点：模拟器按倍速跑，数据时间可能不在"现在"附近。
     * 界面上必须显示**锚点**而不是真实时间，否则会误导人以为数据是实时的。
     */
    dataAnchor: (s) => s.health?.data_anchor ?? null,
    dataMode: (s) => s.health?.data_mode ?? null,
    pendingCount: (s) => s.pending.length,
    /** 红线字段：必须恒为 false。为 true 说明有人绕过了高风险确认。 */
    highRiskAutoExec: (s) => s.health?.high_risk_auto_exec === true,
  },

  actions: {
    async refreshHealth() {
      try {
        this.health = await getHealth()
        this.healthError = null
        this.lastHealthAt = new Date()
      } catch (e) {
        this.healthError = e.message
      }
    },

    async refreshPending() {
      try {
        const d = await listPending()
        this.pending = d?.items ?? []
        this.pendingError = null
      } catch (e) {
        this.pendingError = e.message
      }
    },

    async refreshAll() {
      this.loading = true
      await Promise.all([this.refreshHealth(), this.refreshPending()])
      this.loading = false
    },

    startPolling({ healthMs = 10000, pendingMs = 15000 } = {}) {
      this.stopPolling()
      this._healthTimer = setInterval(() => this.refreshHealth(), healthMs)
      this._pendingTimer = setInterval(() => this.refreshPending(), pendingMs)
    },

    stopPolling() {
      if (this._healthTimer) clearInterval(this._healthTimer)
      if (this._pendingTimer) clearInterval(this._pendingTimer)
      this._healthTimer = null
      this._pendingTimer = null
    },
  },
})
