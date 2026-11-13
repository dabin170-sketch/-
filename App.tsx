import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { aiBusy, aiReady, availableModels, chat, runJob, webgpuSupported } from './ai'
import { api, flushOutbox, kvGet, kvSet, setCsrf } from './api'
import { AgentPanel, TaskModal } from './Panels'
import { Agents, Dashboard, OfficePage, Tasks } from './pages/Main'
import { FlowModal, Flows } from './pages/Flows'
import { Auth, Emergency, Settings, Simulator, Welcome } from './pages/System'
import { Docs, Meetings, Memory, Planner, Projects, Records } from './pages/Work'
import { useLife, useUI } from './store'
import { Badge } from './ui'

const NAV: [string, string, string][] = [
  ['dashboard', '🏠', '대시보드'], ['flows', '🧭', '업무 실행'], ['office', '🏢', 'AI 사무실'], ['tasks', '📋', '업무 센터'], ['agents', '🧑‍💼', '직원 관리'],
  ['meetings', '💬', 'AI 회의실'], ['planner', '📅', '일정·목표'], ['records', '✏️', '기록'], ['projects', '🚀', '프로젝트'],
  ['docs', '📄', '문서 공장'], ['memory', '🧠', '기억 보관소'], ['simulator', '🔮', '시뮬레이터'], ['emergency', '🚨', '비상대책실'], ['settings', '⚙️', '설정'],
]
const PAGES: Record<string, () => JSX.Element | null> = {
  dashboard: Dashboard, flows: Flows, office: OfficePage, tasks: Tasks, agents: Agents, meetings: Meetings, planner: Planner, records: Records,
  projects: Projects, docs: Docs, memory: Memory, simulator: Simulator, emergency: Emergency, settings: Settings,
}

export default function App() {
  const [me, setMe] = useState<any>(null)
  const [bootErr, setBootErr] = useState('')
  const [welcome, setWelcome] = useState(false)
  const loadMe = () => api('/api/me').then((m) => { setCsrf(m.csrf); setMe(m); setBootErr('') }).catch((e) => { setBootErr(e.message); setTimeout(loadMe, 5000) })
  useEffect(() => { loadMe() }, [])
  if (!me) return <div className="min-h-screen flex flex-col items-center justify-center bg-cream text-ink p-6 text-center gap-2">
    <img src="/icon.svg" alt="" className="w-16 h-16 animate-bounce" style={{ imageRendering: 'pixelated' }} />
    <p className="font-bold">사무실 문을 여는 중…</p>{bootErr && <p className="text-sm text-stone-600 max-w-xs">{bootErr} 자동으로 다시 시도합니다.</p>}</div>
  if (!me.authed) return <Auth me={me} onDone={(csrf, first) => { setCsrf(csrf); setWelcome(first); setMe({ ...me, authed: true, setup_needed: false }) }} />
  return <><Shell onAuthLost={loadMe} />{welcome && <Welcome onClose={() => setWelcome(false)} />}</>
}

function Shell({ onAuthLost }: { onAuthLost: () => void }) {
  const { page, setPage, set, ai, setAI, toast, live, liveSync, online } = useUI()
  const { data: st, error } = useLife()
  const qc = useQueryClient()
  const started = useRef(false)
  const recovered = useRef(false)
  const lastSave = useRef(0)
  const running = useRef(false)

  // 인증 만료
  useEffect(() => { if ((error as any)?.status === 401) onAuthLost() }, [error])
  useEffect(() => { set({ online: !(error as any) || (error as any).status !== 0 }) }, [error])

  // 접속 = 세션 시작 → 직원 자동 출근. 이후 20초마다 heartbeat.
  useEffect(() => {
    if (!webgpuSupported()) setAI({ status: 'unsupported' })
    const beat = (start = false) => api(start ? '/api/session/start' : '/api/session/heartbeat', { method: 'POST', body: { ai_ready: aiReady(), ai_models: availableModels() } })
      .then(() => { flushOutbox().then((n) => { if (n) qc.invalidateQueries({ queryKey: ['state'] }) }) }).catch(() => undefined)
    if (!started.current) { started.current = true; beat(true).then(() => qc.invalidateQueries({ queryKey: ['state'] })) }
    const iv = setInterval(() => beat(), 20000)
    const onVisible = () => { if (!document.hidden) { beat(); qc.invalidateQueries({ queryKey: ['state'] }) } }
    const onHide = () => navigator.sendBeacon?.('/api/session/end')   // 닫힐 때 알리되, 실패해도 서버가 heartbeat 만료로 처리
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('pagehide', onHide)
    return () => { clearInterval(iv); document.removeEventListener('visibilitychange', onVisible); window.removeEventListener('pagehide', onHide) }
  }, [])

  // AI 준비 상태가 바뀌면 즉시 서버에 알림 (막혀 있던 AI 업무 재개)
  useEffect(() => { if (ai.status === 'ready' || ai.status === 'off') api('/api/session/heartbeat', { method: 'POST', body: { ai_ready: aiReady(), ai_models: availableModels() } }).catch(() => undefined) }, [ai.status])

  // 실시간 상태 (SSE)
  useEffect(() => {
    let es: EventSource | null = null, rev = -1, timer: any
    const open = () => {
      es = new EventSource('/api/stream')
      es.onmessage = (ev) => {
        const d = JSON.parse(ev.data)
        set({ live: d.agents, liveSync: d.sync })
        if (d.rev !== rev) { rev = d.rev; qc.invalidateQueries({ queryKey: ['state'] }) }
      }
      es.onerror = () => { es?.close(); timer = setTimeout(open, 5000) }
    }
    open()
    return () => { es?.close(); clearTimeout(timer) }
  }, [])

  // IndexedDB 보관본: 서버가 저장 전에 재시작됐다면 기기의 더 최신 상태를 병합 요청
  useEffect(() => {
    if (!st) return
    (async () => {
      if (!recovered.current) {
        recovered.current = true
        const local: any = await kvGet('snapshot')
        if (local && local.boot_id !== st.boot_id && local.rev > st.rev) {
          try { await api('/api/recover', { method: 'POST', body: { state: { rev: local.rev, c: local.c, tasks: local.tasks, approvals: local.approvals } } }); useUI.getState().notify('서버 재시작으로 빠진 변경을 이 기기의 보관본에서 복구했습니다'); qc.invalidateQueries({ queryKey: ['state'] }) } catch { /* 다음 접속에 재시도 */ }
          return
        }
      }
      if (Date.now() - lastSave.current > 4000) { lastSave.current = Date.now(); kvSet('snapshot', { rev: st.rev, boot_id: st.boot_id, c: st.c, tasks: st.tasks, approvals: st.approvals }) }
    })()
  }, [st?.rev])

  // 로컬 AI 작업 큐: 모델 하나로 한 번에 한 건씩
  useEffect(() => {
    const job = st?.ai_jobs?.[0]
    if (!job || running.current || !aiReady() || aiBusy()) return
    running.current = true
    const run = job.kind === 'workflow' ? runJob(job) : chat(job.system, job.prompt).then((text) => ({ text, model: '', ms: 0 }))
    run
      .then((r) => api(`/api/ai/jobs/${job.id}`, { method: 'POST', body: { text: r.text, model: r.model, ms: r.ms } }))
      .catch((e) => api(`/api/ai/jobs/${job.id}`, { method: 'POST', body: { error: String(e?.message ?? e).slice(0, 300) } }).catch(() => undefined))
      .finally(() => { running.current = false; qc.invalidateQueries({ queryKey: ['state'] }) })
  }, [st?.ai_jobs?.[0]?.id, ai.status])

  const Page = PAGES[page] ?? Dashboard
  const pending = st?.approvals.filter((a: any) => a.status === 'PENDING').length ?? 0
  const syncState = liveSync || st?.sync.state
  const sync: any = { SAVED: ['저장됨', 'bg-mint'], PENDING: ['저장 대기', 'bg-sun'], ERROR: ['저장 실패', 'bg-red-200 text-red-900'], LOCAL_ONLY: ['임시 저장', 'bg-coral'] }[syncState as string] ?? ['…', 'bg-cream']
  const present = st ? st.agents.filter((a: any) => (live[a.id] ?? a.status) !== 'OFFLINE').length : 0

  return (
    <div className="min-h-screen bg-cream text-ink md:flex">
      <aside className="md:w-48 md:shrink-0 md:min-h-screen md:border-r-2 border-brown bg-paper md:sticky md:top-0 md:self-start">
        <div className="flex items-center justify-between gap-2 px-3 py-2 border-b-2 border-brown bg-sun">
          <button className="flex items-center gap-2" onClick={() => setPage('dashboard')}><img src="/icon.svg" alt="" className="w-7 h-7" style={{ imageRendering: 'pixelated' }} /><span className="font-extrabold">LIFE OS</span></button>
          <div className="flex gap-1 items-center">
            {!online && <Badge cls="bg-red-200 text-red-900">연결 끊김</Badge>}
            <button onClick={() => setPage('settings')} title="저장 상태"><Badge cls={sync[1]}>{sync[0]}</Badge></button>
            {pending > 0 && <button onClick={() => setPage('tasks')}><Badge cls="bg-coral">승인 {pending}</Badge></button>}
          </div>
        </div>
        <nav className="flex md:flex-col overflow-x-auto md:overflow-visible px-1 py-1 gap-1" aria-label="주 메뉴">
          {NAV.map(([k, icon, label]) => (
            <button key={k} onClick={() => setPage(k)} aria-current={page === k ? 'page' : undefined}
              className={`shrink-0 flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-sm font-semibold border-2 ${page === k ? 'bg-mint border-brown shadow-pxs' : 'border-transparent hover:bg-cream'}`}>
              <span aria-hidden>{icon}</span><span className="whitespace-nowrap">{label}</span>
            </button>
          ))}
        </nav>
        <p className="hidden md:block text-xs text-stone-500 px-3 py-2">출근 {present}/12 · {ai.status === 'ready' ? 'AI 켜짐' : '규칙 기반 모드'}</p>
      </aside>
      <main className="flex-1 min-w-0 p-3 md:p-5 max-w-5xl">
        <h1 className="text-lg font-extrabold mb-3">{NAV.find((n) => n[0] === page)?.[2] ?? '대시보드'}</h1>
        {!st ? <p className="text-sm text-stone-500">{error ? (error as any).message : '불러오는 중…'}</p> : <Page />}
      </main>
      <AgentPanel /><TaskModal /><FlowModal />
      {toast && <div role="status" className={`fixed bottom-4 left-1/2 -translate-x-1/2 z-50 px-3 py-2 rounded-md border-2 border-brown shadow-px text-sm font-semibold max-w-[90vw] ${toast.kind === 'err' ? 'bg-red-200 text-red-900' : 'bg-mint'}`}>{toast.msg}</div>}
    </div>
  )
}
