/** 后端接口清单（与 backend/app/api/ 一一对应）。
 *
 * 所有字段都照抄 `backend/app/schemas/` 里的模型，**不要凭记忆改字段名** ——
 * 后端改了 schema，这里也要跟着改，改动点集中在这一个文件里。
 *
 * ⚠️ 三个已经踩过的坑，写在这里免得下次再踩：
 *  1. `/api/health` 的 `devices` 键是 `total / rooms / high_risk / online`；
 *     而 `/api/devices` 的 `meta.summary` 键是 `devices / rooms / high_risk / capabilities`。
 *     **两个都叫"设备汇总"但键完全不同，互相都没有对方的键**，别共用一套字段映射。
 *  2. `/api/devices/{id}/readings` 有 5000 行上限，默认窗口（24h）会直接 400。
 *     调用时**必须**带上 `metric` 和收窄的 `from/to`。
 *  3. 默认时间窗的锚点是**数据锚点**（模拟器的时间轴），不是 `Date.now()`。
 */

import { del, get, patch, post, postStream, put, upload } from './http'

export const HOME_ID = 1

// ---------------------------------------------------------------- 户型图
/**
 * 当前户型几何。
 *
 * `source: "default"` 表示还没自定义过（用的是 `roomGeometry.js` 里的内置户型）；
 * `"custom"` 表示读的是 `config/floorplan.json`。
 * `background` 非空时，房间填充会变半透明、墙线不再画（墙在图里）。
 * `missing_rooms` 是"库里有、但没画到图上"的房间 —— 它们的设备不会出现在户型图里，
 * 界面应当提示，否则用户会以为设备丢了。
 */
export const getFloorplan = (homeId = HOME_ID) => get('/floorplan', { home_id: homeId })

/** 保存框选。`areas[].code` 必须能在 `rooms` 表里找到（交通空间除外）。 */
export const saveFloorplan = (payload, homeId = HOME_ID) =>
  put(`/floorplan?home_id=${homeId}`, payload)

/**
 * 上传户型底图（PNG/JPEG/WEBP）。
 * 后端会用 Pillow 真解码一次并归一化成 PNG，同时把画布尺寸设成图片尺寸。
 */
export const uploadFloorplanImage = (file, homeId = HOME_ID) =>
  upload(`/floorplan/image?home_id=${homeId}`, file)

/**
 * 删除户型底图 —— **只删图**。
 * 框选、房间、画布尺寸都保留（坐标系没变，框不会错位），删完回到"无底图模式"。
 */
export const removeFloorplanImage = (homeId = HOME_ID) =>
  del(`/floorplan/image?home_id=${homeId}`)

/** 恢复默认。`keepBackground=true` 只清框选、保留底图（图对、框错了的场景）。 */
export const resetFloorplan = (keepBackground = false, homeId = HOME_ID) =>
  post(
    `/floorplan/reset?keep_background=${keepBackground ? 'true' : 'false'}&home_id=${homeId}`,
  )

// ---------------------------------------------------------------- 系统
export const getHealth = () => get('/health')

/** 套房列表（顶部切换用）。返回每套房的房间数/设备数/在线数。 */
export const getHomes = () => get('/homes')

/**
 * 新建套房。
 *
 * **只传名字**：房间由后端先建 3 个占位（客厅/主卧/次卧），
 * 真正的房名在「编辑户型」里拖框时改 —— 那里名字和位置是同一件事，
 * 少一处输入就少一处"两边名字不一致"的坑。
 */
export const createHome = (name) => post('/homes', { name })

/** 给套房改名（「编辑房子」里改，和房间名不是一回事）。 */
export const renameHome = (homeId, name) => patch(`/homes/${homeId}`, { name })

/**
 * 删除房间（连同房间里的设备与历史数据，**不可恢复**）。
 * 户型编辑器里删掉房间框并保存时调用。
 */
export const deleteRoom = (homeId, code) =>
  del(`/homes/${homeId}/rooms/${encodeURIComponent(code)}?confirm=true`)

/** 在套房里新增房间（户型编辑器用）。编码由后端生成 `room_N`。 */
export const createRoom = (homeId, name) => post(`/homes/${homeId}/rooms`, { name })

/**
 * 删除套房（连同设备与全部历史数据，**不可恢复**）。
 * `confirm=true` 由前端固定带上 —— 后端不带会 400，防止误拼 URL 就删库。
 */
export const deleteHome = (homeId) => del(`/homes/${homeId}?confirm=true`)

// ---------------------------------------------------------------- 态势
export const getRooms = (homeId = HOME_ID) => get(`/homes/${homeId}/rooms`)

// ---------------------------------------------------------------- 设备
export const getDevices = (params = {}) => get('/devices', params)
export const getDevice = (id) => get(`/devices/${id}`)
export const getDeviceReadings = (id, params = {}) => get(`/devices/${id}/readings`, params)
export const getDeviceCapabilities = (id) => get(`/devices/${id}/capabilities`)

/**
 * 给设备下发一条指令。
 *
 * ⚠️ 返回 `status="executed"` 只代表**指令已下发并留痕**，
 * **不代表设备状态已经变了** —— 那要看返回体里的 `acknowledged`（设备回执）。
 * 门锁 / 燃气阀 / 电气总闸永远只会返回 `status="pending_confirm"`，
 * 因为后端 `executor.create_command()` 会把高风险指令强制改写成待确认。
 */
export const sendDeviceCommand = (deviceId, { capability, value, issuedBy = 'dashboard', note = null }) =>
  post(`/devices/${deviceId}/command`, {
    capability,
    value,
    issued_by: issuedBy,
    note,
  })

/** 某台设备的指令记录（含设备回执）。用来回答"点了开关到底执行成没成"。 */
export const listDeviceCommands = (deviceId, params = {}) =>
  get(`/devices/${deviceId}/commands`, params)

// ---------------------------------------------------------------- 设备管理
/**
 * 可添加的预设设备清单。
 *
 * ⚠️ 用户只能从这份清单里选，不能自定义设备类型/能力。
 * 返回体里的 `high_risk` 是**只读展示**：后端按 `device_type` 推导，
 * 新增请求里根本没有这个字段，传了也不生效（红线不能由用户设置）。
 */
export const listDevicePresets = () => get('/device-presets')

/** 按预设新增一台设备。⚠️ 生成器约 30 秒后才会开始为它产数据。 */
export const createDevice = ({ presetKey, roomId, name = null, code = null }) =>
  post('/devices', { preset_key: presetKey, room_id: roomId, name, code })

/**
 * 启用 / 停用设备。
 *
 * ⚠️ 停用是**软删**（`devices.enabled = 0`）：设备会从接口和生成器里同时消失，
 * 但历史读数与指令审计全部保留，随时可以重新启用。
 * 不做硬删是因为外键会挡住（device_commands / sensor_events 是 NO ACTION），
 * 而且会在 device_readings 留下孤儿行。
 */
export const setDeviceEnabled = (deviceId, enabled) =>
  post(`/devices/${deviceId}/enabled`, { enabled })

/**
 * 已停用（软删）的设备。
 *
 * ⚠️ 必须单独查：`/api/devices` 走设备目录，而目录的 SQL 带 `WHERE d.enabled = 1`，
 * 所以**停用之后设备会从列表里消失** —— 没有这个接口，用户就再也点不到"恢复"了。
 */
export const listDisabledDevices = () => get('/disabled-devices')

/**
 * **彻底删除**一台设备（不可逆）。
 *
 * ⚠️ 与「停用」的区别：
 * - 停用 = 软删，设备从接口/生成器消失，但一切历史都留着，随时可恢复
 * - 彻底删除 = 真删库，**连指令审计记录（含高风险动作的人工确认留痕）一起删**，不可恢复
 *
 * 后端要求：设备必须**先停用**，且必须传 `acknowledgeAuditLoss: true`，
 * 否则分别返回 409 / 400。返回体里有**逐表删除行数**和**受影响的场景**，
 * 界面必须如实展示 —— 不能只说一句"删除成功"。
 */
export const purgeDevice = (deviceId) =>
  post(`/devices/${deviceId}/purge`, { acknowledge_audit_loss: true })

// ---------------------------------------------------------------- 能耗
export const getEnergySummary = (params = {}) => get('/energy/summary', params)

// ---------------------------------------------------------------- 对话
/**
 * 一句话交给多 Agent 编排。
 *
 * ⚠️ `home_id` 必须跟着**当前套房**走（多套房是已落地功能，写死 1 会让对话
 * 一直查演示家庭的房）。`thread_id` 用于延续同一轮会话 —— 高风险的 interrupt
 * 恢复就是靠它定位到被挂起的那次运行。
 */
export const chatOnce = (question, threadId = null, homeId = HOME_ID) =>
  post('/chat', { question, thread_id: threadId, home_id: homeId })

export const chatStream = (question, threadId = null, homeId = HOME_ID, opts = {}) =>
  postStream('/chat/stream', { question, thread_id: threadId, home_id: homeId }, opts)

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
/**
 * 编排（只算不做）。
 *
 * `homeId` 决定模型**能看到哪些设备** —— 后端会按它把设备目录收窄后再喂给模型。
 * 不传的话后端视为"不过滤"，多套房下会编排到别的套房的设备上。
 */
export const previewScene = (query, dryRun = false, homeId = HOME_ID) =>
  post('/scenes/preview', { query, dry_run_execute: dryRun, home_id: homeId })

export const planScene = (actions, sceneName = '', homeId = HOME_ID) =>
  post('/scenes/plan', { actions, scene_name: sceneName, home_id: homeId })

export const listScenes = (homeId = HOME_ID) => get('/scenes', { home_id: homeId })
export const createScene = (payload) => post('/scenes', payload)
export const runScene = (sceneId) => post(`/scenes/${sceneId}/run`)

/**
 * 编辑场景（改名 / 改描述 / 改动作）。
 *
 * `actions` 不传 = 动作不变；传**空数组**会被后端 422 拒绝（空场景没法执行）。
 * 改动作会重新走编排校验，校验不过的动作**整条请求 422** 并返回 `rejected`，
 * 不会静默丢掉 —— 界面要把被剔除的条目和原因显示出来。
 */
export const updateScene = (sceneId, payload) => put(`/scenes/${sceneId}`, payload)

/** 收藏 / 取消收藏。收藏的场景会出现在房屋总览的「用户收藏的场景」卡片里。 */
export const setSceneFavorite = (sceneId, favorite) =>
  patch(`/scenes/${sceneId}/favorite`, { favorite })

/** 删除场景（连同执行记录，不可恢复）。 */
export const deleteScene = (sceneId) => del(`/scenes/${sceneId}`)

// ---------------------------------------------------------------- 知识库
export const searchKnowledge = (q, params = {}) => get('/knowledge/search', { q, ...params })
export const listDocuments = () => get('/knowledge/documents')
export const getKnowledgeStatus = () => get('/knowledge/status')

// ---------------------------------------------------------------- 安防 / 照护 / 建议
/**
 * 告警列表。每条都带 `detail`（判据 + 证据 + 建议处置，纯文本多行）。
 *
 * ⚠️ 后端会在返回前**先跑一次幂等推导**（`sync=true`），所以打开页面就有数据；
 * 返回体里的 `sync` 字段说明这次扫了几条事件、新建/恢复了多少告警 ——
 * 答辩被问"这条告警哪来的"时，答案就在响应里。
 * 另外 `total` 与 `by_level` 都受时间窗过滤（口径一致），别用 by_level 当全量。
 */
export const listAlarms = (params = {}) => get('/security/alarms', params)

/**
 * 人工确认一条告警。
 *
 * ⚠️ 确认只代表"人知道了"，**不代表设备动作已执行**。
 * 关燃气阀/锁门必须走待确认通道，那是另一条链路。
 */
export const ackAlarm = (id, acknowledgedBy = null) =>
  post(`/security/alarms/${id}/ack`, { acknowledged_by: acknowledgedBy })

/**
 * 照护观察。
 *
 * ⚠️ 先看 `coverage`，再看 `items`：`sufficient=false` 表示这段时间的感知数据
 * 覆盖不足，**不能据此推断活动节律**（接口会返回「数据不足，无法判断」而不是编一个）。
 * `confidence` 是数据可信度，不是健康风险。
 */
export const listCareObservations = (params = {}) => get('/care/observations', params)

/** 主动建议。按打扰等级排序；`reason` 里是规则名与命中数值。 */
export const listSuggestions = (params = {}) => get('/suggestions', params)
export const dismissSuggestion = (id) => post(`/suggestions/${id}/dismiss`)

/** 采纳建议。⚠️ 只记状态，后端**不会下发任何设备指令**。 */
export const acceptSuggestion = (id) => post(`/suggestions/${id}/accept`)
