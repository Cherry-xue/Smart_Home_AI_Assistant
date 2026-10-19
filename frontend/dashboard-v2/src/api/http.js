/** 极薄的 fetch 封装。
 *
 * 为什么不用 axios：这个项目只需要同源 GET/POST（Vite 已经把 /api 代理到后端），
 * 拦截器、取消、上传进度这些一个都用不上，多一个依赖只增加装包时间。
 * 唯一的"非标准"需求是 SSE —— 那也必须直接用 fetch + ReadableStream
 * （axios 在浏览器里靠 XHR，读不了流）。
 *
 * ⚠️ 这一层是从旧大屏（frontend/dashboard）**迁移**过来的，不是重写的。
 *   它里面没有业务逻辑，只有踩坑换来的约定：把 FastAPI 的 detail 抬到表层、
 *   空参数自动剔除、SSE 必须用流。重写只会把这些坑再踩一遍。
 */

const BASE = '/api'

/** 统一的错误处理：把后端 FastAPI 的 `detail` 抬到表层，免得每个页面各写一遍。 */
async function parse(res) {
  const text = await res.text()
  let data = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }
  if (!res.ok) {
    const detail =
      (data && (data.detail ?? data.message)) ||
      res.statusText ||
      `HTTP ${res.status}`
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    err.status = res.status
    err.payload = data
    throw err
  }
  return data
}

function qs(params = {}) {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    // 空值直接不发：后端对 "from=" 这类空串会当成非法时间戳
    if (v === undefined || v === null || v === '') continue
    if (Array.isArray(v)) {
      v.forEach((x) => sp.append(k, x))
    } else {
      sp.append(k, v)
    }
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export function get(path, params) {
  return fetch(`${BASE}${path}${qs(params)}`, {
    headers: { Accept: 'application/json' },
  }).then(parse)
}

export function post(path, body) {
  return fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(parse)
}

export function put(path, body) {
  return fetch(`${BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(parse)
}

/** PATCH（目前只用于给套房改名）。 */
export function patch(path, body) {
  return fetch(`${BASE}${path}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(parse)
}

/**
 * DELETE。
 *
 * ⚠️ 删除类接口一律把"确认"做成查询参数（`?confirm=true`），
 *    这样误拼一个 URL 不会真的删掉东西 —— 后端不带 confirm 会直接 400。
 */
export function del(path) {
  return fetch(`${BASE}${path}`, { method: 'DELETE' }).then(parse)
}

/**
 * 上传单个文件（multipart/form-data）。
 *
 * ⚠️ **不要手写 Content-Type**：必须让浏览器自己带上 boundary，
 *    手写成 `multipart/form-data` 会丢掉 boundary，后端解析直接 400。
 */
export function upload(path, file, field = 'file') {
  const fd = new FormData()
  fd.append(field, file)
  return fetch(`${BASE}${path}`, { method: 'POST', body: fd }).then(parse)
}

/**
 * 消费 SSE 流（POST + text/event-stream）。
 *
 * ⚠️ 为什么不用 EventSource：它只支持 GET、不能带请求体，
 * 而对话需要把用户这句话 POST 过去，还要能被 AbortController 真停掉。
 *
 * @returns { promise, abort }
 */
export function postStream(path, body, { onEvent, signal } = {}) {
  const controller = new AbortController()
  const abort = () => controller.abort()

  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', abort, { once: true })
  }

  const run = async () => {
    const res = await fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body ?? {}),
      signal: controller.signal,
    })

    if (!res.ok || !res.body) {
      await parse(res)
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let idx
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const chunk = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data:')) continue
          const raw = line.slice(5).trim()
          if (!raw) continue
          try {
            onEvent?.(JSON.parse(raw))
          } catch {
            onEvent?.({ type: 'raw', data: raw })
          }
        }
      }
    }

    const tail = buffer.trim()
    if (tail.startsWith('data:')) {
      try {
        onEvent?.(JSON.parse(tail.slice(5).trim()))
      } catch {
        /* 忽略残帧 */
      }
    }
  }

  const promise = run().catch((e) => {
    if (e.name === 'AbortError') return
    throw e
  })

  return { promise, abort }
}
