import { createRouter, createWebHistory } from 'vue-router'
import DashboardLayout from '@/layout/DashboardLayout.vue'

/** 页面表：侧栏菜单就是从这张表生成的，加页面只改这里。 */
export const pageRoutes = [
  {
    path: '/overview',
    name: 'overview',
    component: () => import('@/views/Overview.vue'),
    meta: { title: '态势总览', icon: 'Odometer' },
  },
  {
    path: '/devices',
    name: 'devices',
    component: () => import('@/views/Devices.vue'),
    meta: { title: '设备', icon: 'Cpu' },
  },
  {
    path: '/energy',
    name: 'energy',
    component: () => import('@/views/Energy.vue'),
    meta: { title: '能耗', icon: 'Lightning' },
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('@/views/Chat.vue'),
    meta: { title: 'AI 对话', icon: 'ChatDotRound' },
  },
  {
    path: '/scenes',
    name: 'scenes',
    component: () => import('@/views/Scenes.vue'),
    meta: { title: '场景编排', icon: 'MagicStick' },
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('@/views/Knowledge.vue'),
    meta: { title: '知识库', icon: 'Document' },
  },
  {
    path: '/system',
    name: 'system',
    component: () => import('@/views/System.vue'),
    meta: { title: '系统状态', icon: 'Monitor' },
  },
]

const routes = [
  {
    path: '/',
    component: DashboardLayout,
    redirect: '/overview',
    children: [...pageRoutes, { path: '', redirect: '/overview' }],
  },
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  document.title = `${to.meta.title ?? '智能家居'} · 态势大屏`
})

export default router
