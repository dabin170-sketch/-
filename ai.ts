// 브라우저 로컬 AI (WebLLM). 사용자가 직접 켤 때만 모델을 내려받는다.
// 라이브러리는 번들에 넣지 않고 필요할 때 CDN에서 불러온다 → 켜지 않으면 추가 다운로드가 없다.
const CDN = 'https://esm.run/@mlc-ai/web-llm'

export type ModelInfo = { id: string; vram: number; low: boolean }
let lib: any = null
let engine: any = null
let engineModel = ''
let engineVram = 0
let busy = false

// ---------------------------------------------------------------- Ollama (내 컴퓨터에서 실행하는 로컬 모델, 선택)
export type OllamaCfg = { url: string; light: string; strong: string; on: boolean }
export function ollamaCfg(): OllamaCfg {
  try { return { url: 'http://localhost:11434', light: '', strong: '', on: false, ...JSON.parse(localStorage.getItem('lifeos.ollama') || '{}') } }
  catch { return { url: 'http://localhost:11434', light: '', strong: '', on: false } }
}
export function saveOllama(c: OllamaCfg) { localStorage.setItem('lifeos.ollama', JSON.stringify(c)) }
let ollamaOk = false
export const ollamaReady = () => ollamaOk

/** Ollama 서버에 설치된 모델 목록. 연결되면 이 브라우저에서만 쓴다 (서버로 데이터가 가지 않음). */
export async function ollamaModels(url: string): Promise<{ name: string; size: number }[]> {
  const r = await fetch(url.replace(/\/$/, '') + '/api/tags')
  if (!r.ok) throw new Error(`Ollama 응답 ${r.status}`)
  const j = await r.json()
  ollamaOk = true
  return (j.models ?? []).map((m: any) => ({ name: m.name, size: m.size ?? 0 }))
}
export function setOllamaReady(v: boolean) { ollamaOk = v }

/** 작업에 쓸 수 있는 모델 (단계별 라우팅용). 실제로 불러온 모델만 알린다. */
export function availableModels(): { light?: string; strong?: string } {
  const out: { light?: string; strong?: string } = {}
  const o = ollamaCfg()
  if (o.on && ollamaOk) { if (o.light) out.light = 'ollama:' + o.light; if (o.strong) out.strong = 'ollama:' + o.strong }
  if (engine) { const tier = engineVram && engineVram <= 1500 ? 'light' : 'strong'; if (!out[tier]) out[tier] = 'webllm:' + engineModel }
  return out
}

async function ollamaChat(model: string, system: string, prompt: string, timeoutMs: number): Promise<string> {
  const o = ollamaCfg()
  const ctl = new AbortController()
  const t = setTimeout(() => ctl.abort(), timeoutMs)
  try {
    const r = await fetch(o.url.replace(/\/$/, '') + '/api/chat', {
      method: 'POST', signal: ctl.signal, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, stream: false, options: { temperature: 0.3, num_predict: 400 },
        messages: [{ role: 'system', content: system }, { role: 'user', content: prompt }] }),
    })
    if (!r.ok) throw new Error(`Ollama 응답 ${r.status}`)
    const j = await r.json()
    return String(j.message?.content ?? '').trim()
  } finally { clearTimeout(t) }
}

/** 서버가 정한 모델(job.model)로 실행한다. 모델이 없으면 실패를 그대로 알린다 (다른 결과로 위장하지 않음). */
export async function runJob(job: { model?: string; system: string; prompt: string }, timeoutMs = 150000): Promise<{ text: string; model: string; ms: number }> {
  const want = job.model || availableModels().strong || availableModels().light || ''
  const t0 = performance.now()
  if (busy) throw new Error('다른 추론이 진행 중입니다')
  busy = true
  try {
    let text = ''
    if (want.startsWith('ollama:')) text = await ollamaChat(want.slice(7), job.system, job.prompt, timeoutMs)
    else if (want.startsWith('webllm:') && engine && want.slice(7) === engineModel) { busy = false; text = await chat(job.system, job.prompt, timeoutMs); busy = true }
    else throw new Error(`요청된 모델(${want || '없음'})이 이 브라우저에 준비되어 있지 않습니다`)
    return { text, model: want, ms: Math.round(performance.now() - t0) }
  } finally { busy = false }
}

export function webgpuSupported(): boolean {
  return typeof navigator !== 'undefined' && 'gpu' in navigator
}

export function deviceHint(): string {
  const mem = (navigator as any).deviceMemory
  const mobile = /Android|iPhone|iPad/i.test(navigator.userAgent)
  if (!webgpuSupported()) return '이 브라우저는 WebGPU를 지원하지 않아 로컬 AI를 실행할 수 없습니다. 규칙 기반 모드로 동작합니다.'
  if (mobile || (mem && mem <= 4)) return '메모리가 적은 기기입니다. 가장 작은 모델만 권장하며, 실행이 느리거나 실패할 수 있습니다.'
  return 'WebGPU 사용 가능. 처음 켤 때 모델 파일(수백 MB~1GB대)을 내려받아 브라우저에 저장합니다.'
}

/** 실행 가능한 작은 모델 목록 (라이브러리가 제공하는 목록에서 필요한 VRAM이 작은 순서로). */
export async function listModels(): Promise<ModelInfo[]> {
  if (!webgpuSupported()) throw new Error('WebGPU 미지원')
  if (!lib) lib = await import(/* @vite-ignore */ CDN)
  const list: any[] = lib.prebuiltAppConfig?.model_list ?? []
  return list
    .filter((m) => (m.model_type === undefined || m.model_type === 0) && typeof m.vram_required_MB === 'number')
    .filter((m) => m.vram_required_MB <= 2600 && /instruct|chat|-it-|Qwen|SmolLM|gemma|Phi|Llama/i.test(m.model_id))
    .map((m) => ({ id: m.model_id as string, vram: Math.round(m.vram_required_MB), low: !!m.low_resource_required }))
    .sort((a, b) => (Number(/qwen|gemma/i.test(b.id)) - Number(/qwen|gemma/i.test(a.id))) || a.vram - b.vram)
    .slice(0, 14)
}

export async function loadModel(id: string, onProgress: (text: string, pct: number) => void, vram = 0) {
  if (!lib) lib = await import(/* @vite-ignore */ CDN)
  if (engine) { try { await engine.unload() } catch { /* */ } engine = null }
  engineModel = id
  engineVram = vram
  engine = await lib.CreateMLCEngine(id, {
    initProgressCallback: (p: any) => onProgress(p.text ?? '', Math.round((p.progress ?? 0) * 100)),
  })
}

export async function unloadModel() {
  if (engine) { try { await engine.unload() } catch { /* */ } }
  engine = null
}

export const aiReady = () => !!engine || (ollamaCfg().on && ollamaOk && !!(ollamaCfg().light || ollamaCfg().strong))
export const aiBusy = () => busy

/** 모델 하나를 모든 직원이 공유한다 — 한 번에 한 건만 추론. */
export async function chat(system: string, prompt: string, timeoutMs = 120000): Promise<string> {
  if (!engine) {
    const m = availableModels()
    const pick = m.strong || m.light
    if (pick?.startsWith('ollama:')) return ollamaChat(pick.slice(7), system, prompt, timeoutMs)
    throw new Error('모델이 로드되지 않았습니다')
  }
  if (busy) throw new Error('다른 추론이 진행 중입니다')
  busy = true
  try {
    const run = engine.chat.completions.create({
      messages: [{ role: 'system', content: system }, { role: 'user', content: prompt }],
      max_tokens: 400, temperature: 0.4,
    })
    const timer = new Promise<never>((_, rej) => setTimeout(() => rej(new Error('추론 시간 초과')), timeoutMs))
    const res: any = await Promise.race([run, timer])
    return String(res.choices?.[0]?.message?.content ?? '').trim()
  } catch (e) {
    try { await engine.interruptGenerate?.() } catch { /* */ }
    throw e
  } finally {
    busy = false
  }
}
