/** 后端接口清单（与 backend/app/api/ 一一对应）。
 *
 * 所有字段都照抄 `app/schemas/` 里的模型，**不要凭记忆改字段名** ——
 * 后端改了 schema，这里也要跟着改，改动点集中在这一个文件里。
 */

import { get, post, postStream } from './http'

export const HOME_ID = 1

// ---------------------------------------------------------------- 系统
export const getHealth = () => get('/health')

// ---------------------------------------------------------------- 态势
export const getRooms = (homeId = HOME_ID) => get(`/homes/${homeId}/rooms`)

// ---------------------------------------------------------------- 设备
export const getDevices = (params = {}) => get('/devices', params)
export const getDevice = (id) => get(`/devices/${id}`)
export const getDeviceReadings = (id, params = {}) => get(`/devices/${id}/readings`, params)
export const getDeviceCapabilities = (id) => get(`/devices/${id}/capabilities`)

// ---------------------------------------------------------------- 能耗
export const getEnergySummary = (params = {}) => get('/energy/summary', params)

// ---------------------------------------------------------------- 对话
export const chatOnce = (question, threadId = null) =>
  post('/chat', { question, thread_id: threadId, home_id: HOME_ID })

/** SSE 流式对话；返回 { promise, abort } */
export const chatStream = (question, threadId = null, opts = {}) =>
  postStream(
    '/chat/stream',
    { question, thread_id: threadId, home_id: HOME_ID },
    opts,
  )

// ---------------------------------------------------------------- 人工确认
export const listPending = (includeExpired = false) =>
  get('/confirmations/pending', { include_expired: includeExpired })

export const decidePending = (threadId, { choice, interventionIndex = 0, note = '' }) =>
  post(`/confirmations/${encodeURIComponent(threadId)}`, {
    choice,
    intervention_index: interventionIndex,
    note,
  })

// ---------------------------------------------------------------- 场景
export const previewScene = (query, dryRun = false) =>
  post('/scenes/preview', { query, dry_run_execute: dryRun })

export const planScene = (actions, sceneName = '') =>
  post('/scenes/plan', { actions, scene_name: sceneName })

export const listScenes = () => get('/scenes')
/** 保存一个场景（编排完要先落库才能执行——执行记录要挂在 scene 上） */
export const createScene = (payload) => post('/scenes', payload)
export const runScene = (sceneId) => post(`/scenes/${sceneId}/run`)

// ---------------------------------------------------------------- 知识库
export const searchKnowledge = (q, params = {}) => get('/knowledge/search', { q, ...params })
export const listDocuments = () => get('/knowledge/documents')
export const getKnowledgeStatus = () => get('/knowledge/status')
