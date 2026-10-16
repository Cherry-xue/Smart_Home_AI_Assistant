/** 极薄的 fetch 封装。
 *
 * 为什么不用 axios：这个项目只需要同源 GET/POST（Vite 已经把 /api 代理到后端），
 * 拦截器、取消、上传进度这些一个都用不上，多一个依赖只增加装包时间。
 * 唯一的"非标准"需求是 SSE —— 那也必须直接用 fetch + ReadableStream
 * （axios 在浏览器里靠 XHR，读不了流）。
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

/**
 * 消费 SSE 流（POST + text/event-stream）。
 *
 * ⚠️ 为什么不用 EventSource：它只支持 GET、不能带请求体，
 * 而对话需要把用户这句话 POST 过去，还要能被 AbortController 真停掉。
 *
 * @returns 一个取消函数
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
      // 出错也要走统一解析，让页面上看到后端给的 detail
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

      // SSE 以空行分隔事件
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

    // 收尾：有些实现最后一块不带空行
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
