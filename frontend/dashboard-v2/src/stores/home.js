/** 当前套房（多套房切换）。
 *
 * 为什么要单独一个 store
 * ----------------------
 * `home_id` 会出现在**每一个**取数调用里（房间、设备、户型图）。如果让每个页面
 * 各存一份"当前是哪套房"，切换时必然有一半页面还停在上套房 —— 那种"数据看着对
 * 但其实是别人家"的串台，比加载失败难查得多。
 *
 * 所以：**唯一真相在这里**，页面只读 `home.currentId`，切换只调 `setCurrent()`。
 *
 * ⚠️ 选择记在 localStorage：刷新页面还停在刚才那套房，而不是跳回 1 号。
 *    存的是 id 而不是下标 —— 套房的增删不会让选择错位。
 */

import { defineStore } from 'pinia'
import { createHome, deleteHome, getHomes } from '@/api'

const LS_KEY = 'smart_home.current_home_id'

export const useHomeStore = defineStore('home', {
  state: () => ({
    homes: [],
    currentId: Number(localStorage.getItem(LS_KEY)) || 1,
    loading: false,
    error: null,
    /** 户型被编辑过就 +1。总览页靠它重新取几何 —— 编辑器现在挂在顶栏，
     *  和总览页不是父子关系，用 store 里的版本号比事件总线更直白。 */
    floorplanRev: 0,
  }),

  getters: {
    current: (s) => s.homes.find((h) => h.id === s.currentId) || null,
    currentName: (s) => s.homes.find((h) => h.id === s.currentId)?.name || '当前套房',
    /** 只有一套房时不必显示切换器 */
    multiple: (s) => s.homes.length > 1,
  },

  actions: {
    async refresh() {
      this.loading = true
      try {
        const d = await getHomes()
        this.homes = d?.items ?? []
        this.error = null
        // 原来选的那套没了（被删了/换库了）→ 回落到第一套，不要停在一个不存在的 id 上
        if (this.homes.length && !this.homes.some((h) => h.id === this.currentId)) {
          this.setCurrent(this.homes[0].id)
        }
      } catch (e) {
        this.error = e.message
      } finally {
        this.loading = false
      }
    },

    /** 户型保存/上传/重置后调用，通知展示方重画 */
    bumpFloorplan() {
      this.floorplanRev += 1
    },

    setCurrent(id) {
      this.currentId = id
      try {
        localStorage.setItem(LS_KEY, String(id))
      } catch {
        /* 隐私模式下写不了，不影响使用 */
      }
    },

    /**
     * 新建套房，成功后**自动切过去**。
     *
     * 为什么不建完就留在原地：用户刚点"新建"，接下来一定是要去配置它
     * （添加设备、画户型）。留在旧套房会让人以为没建成功。
     */
    async add(name) {
      const res = await createHome(name)
      await this.refresh()
      this.setCurrent(res.home_id)
      return res
    },

    /**
     * 删除套房。
     *
     * ⚠️ 删的是**当前这套**时，`refresh()` 会把 currentId 回落到第一套；
     *    页面 watch 到 currentId 变化会自动重新取数。删的不是当前这套，
     *    currentId 不变 —— 页面本来就不该刷新（数据没变）。
     */
    async remove(id) {
      const res = await deleteHome(id)
      await this.refresh()
      return res
    },
  },
})
