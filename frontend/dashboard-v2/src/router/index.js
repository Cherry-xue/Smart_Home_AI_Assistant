/** 路由：左侧菜单 8 项与之一一对应。
 *
 * 8 页已全部实现（AI对话 / 场景编排 / 知识库 / 系统状态 是最后一批补的，
 * 内容照旧大屏 dashboard 迁移并统一到 v2 的浅色细线风格）。
 */

import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/overview' },
  {
    path: '/overview',
    name: 'overview',
    component: () => import('@/pages/overview/index.vue'),
    meta: { title: '房屋总览', ready: true },
  },
  { path: '/devices', name: 'devices', component: () => import('@/pages/devices/index.vue'), meta: { title: '设备', ready: true } },
  { path: '/care', name: 'care', component: () => import('@/pages/care/index.vue'), meta: { title: '照护与安防', ready: true } },
  { path: '/energy', name: 'energy', component: () => import('@/pages/energy/index.vue'), meta: { title: '能耗', ready: true } },
  { path: '/chat', name: 'chat', component: () => import('@/pages/chat/index.vue'), meta: { title: 'AI对话', ready: true } },
  { path: '/scenes', name: 'scenes', component: () => import('@/pages/scenes/index.vue'), meta: { title: '场景编排', ready: true } },
  { path: '/knowledge', name: 'knowledge', component: () => import('@/pages/knowledge/index.vue'), meta: { title: '知识库', ready: true } },
  { path: '/system', name: 'system', component: () => import('@/pages/system/index.vue'), meta: { title: '系统状态', ready: true } },
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
