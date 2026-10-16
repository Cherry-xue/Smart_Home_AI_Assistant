/** 全局系统状态：健康信息 + 待确认事项。
 *
 * 为什么要单独一个 store：顶栏要显示"在线设备数 / 数据锚点 / 待确认红点"，
 * 这些是所有页面共用的；如果每个页面各查一次，轮询会变成 N 倍。
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
    // 上一次成功刷新的时间（顶栏用，也用于"是不是卡住了"的判断）
    lastHealthAt: null,
    lastPendingAt: null,
    _healthTimer: null,
    _pendingTimer: null,
  }),

  getters: {
    online: (s) => s.health?.status === 'ok',
    deviceStats: (s) => s.health?.devices ?? {},
    /** 数据锚点：模拟器按倍速跑，数据时间可能不在"现在"附近，界面上必须显式提示 */
    dataAnchor: (s) => s.health?.data_anchor ?? null,
    pendingCount: (s) => s.pending.length,
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
        this.lastPendingAt = new Date()
      } catch (e) {
        this.pendingError = e.message
      }
    },

    async refreshAll() {
      this.loading = true
      await Promise.all([this.refreshHealth(), this.refreshPending()])
      this.loading = false
    },

    /** 开启轮询（在布局挂载时调用一次即可） */
    startPolling({ healthMs = 15000, pendingMs = 10000 } = {}) {
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
