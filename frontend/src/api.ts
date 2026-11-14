// 서버 통신 + IndexedDB(오프라인 대기열·상태 보관본)
let csrf = ''
export const setCsrf = (t: string) => { csrf = t || '' }

export class ApiError extends Error {
  status: number
  constructor(msg: string, status: number) { super(msg); this.status = status }
}

export async function api(path: string, opts: { method?: string; body?: any; headers?: Record<string, string> } = {}) {
  const method = opts.method ?? 'GET'
  const headers: Record<string, string> = { ...(opts.headers ?? {}) }
  const isForm = opts.body instanceof FormData
  if (opts.body !== undefined && !isForm) headers['Content-Type'] = 'application/json'
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf
  let res: Response
  try {
    res = await fetch(path, {
      method, headers, credentials: 'same-origin',
      body: opts.body === undefined ? undefined : isForm ? opts.body : JSON.stringify(opts.body),
    })
  } catch {
    throw new ApiError('서버에 연결할 수 없습니다. (무료 서버가 잠들어 있으면 깨어나는 데 1분쯤 걸립니다)', 0)
  }
  if (!res.ok) {
    let detail = ''
    try { const j = await res.json(); detail = typeof j.detail === 'string' ? j.detail : '입력값을 확인하세요.' } catch { /* 본문 없음 */ }
    throw new ApiError(detail || `요청 실패 (${res.status})`, res.status)
  }
  return res.json()
}

export const newId = (prefix: string) =>
  prefix + '_' + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '').slice(0, 16) : Math.random().toString(36).slice(2, 14) + Date.now().toString(36))

// ---------------------------------------------------------------- IndexedDB
let dbp: Promise<IDBDatabase> | null = null
function db(): Promise<IDBDatabase> {
  if (!dbp) {
    dbp = new Promise((resolve, reject) => {
      const req = indexedDB.open('lifeos', 1)
      req.onupgradeneeded = () => {
        req.result.createObjectStore('kv')
        req.result.createObjectStore('outbox', { keyPath: 'key' })
      }
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => reject(req.error)
    })
  }
  return dbp
}
function tx<T>(store: string, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest): Promise<T> {
  return db().then((d) => new Promise<T>((resolve, reject) => {
    const r = fn(d.transaction(store, mode).objectStore(store))
    r.onsuccess = () => resolve(r.result as T)
    r.onerror = () => reject(r.error)
  }))
}
export const kvGet = <T,>(k: string) => tx<T | undefined>('kv', 'readonly', (s) => s.get(k)).catch(() => undefined)
export const kvSet = (k: string, v: any) => tx('kv', 'readwrite', (s) => s.put(v, k)).catch(() => undefined)
export const outboxAll = () => tx<any[]>('outbox', 'readonly', (s) => s.getAll()).catch(() => [] as any[])

/** 데이터 변경 요청. 서버에 닿지 않으면 IndexedDB 대기열에 넣고, 다시 연결되면 같은 ID로 재전송한다. */
export async function mutate(method: 'POST' | 'PUT' | 'DELETE', path: string, body?: any) {
  try {
    return await api(path, { method, body })
  } catch (e) {
    if (e instanceof ApiError && e.status === 0 && path.startsWith('/api/c/')) {
      await tx('outbox', 'readwrite', (s) => s.put({ key: newId('q'), ts: Date.now(), method, path, body }))
      return { ...(body ?? {}), queued: true }
    }
    throw e
  }
}

export async function flushOutbox(): Promise<number> {
  const items = (await outboxAll()).sort((a, b) => a.ts - b.ts)
  let sent = 0
  for (const it of items) {
    try {
      await api(it.path, { method: it.method, body: it.body })
    } catch (e) {
      if (e instanceof ApiError && (e.status === 0 || e.status === 401 || e.status === 503)) break // 아직 연결 안 됨
      // 4xx: 서버가 거절한 변경은 버린다 (무한 재시도 방지)
    }
    await tx('outbox', 'readwrite', (s) => s.delete(it.key))
    sent++
  }
  return sent
}

export async function download(path: string, fallbackName: string) {
  const res = await fetch(path, { credentials: 'same-origin' })
  if (!res.ok) {
    let d = ''
    try { d = (await res.json()).detail } catch { /* */ }
    throw new ApiError(d || '내려받기 실패', res.status)
  }
  const cd = res.headers.get('content-disposition') || ''
  const m = /filename\*=UTF-8''([^;]+)/.exec(cd)
  const name = m ? decodeURIComponent(m[1]) : fallbackName
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url; a.download = name
  document.body.appendChild(a); a.click(); a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 5000)
}
