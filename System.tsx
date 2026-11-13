import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ModelInfo, OllamaCfg, deviceHint, listModels, loadModel, ollamaCfg, ollamaModels, saveOllama, setOllamaReady, unloadModel, webgpuSupported } from '../ai'
import { api, download, outboxAll, setCsrf } from '../api'
import { Crud } from '../Crud'
import { act, useLife, useRefresh, useUI } from '../store'
import { Badge, Bar, Btn, Card, Empty, Lines, fmtTime, input } from '../ui'

// ------------------------------------------------------------------ 인생 시뮬레이터
export function Simulator() {
  const { data: st } = useLife()
  const [f, setF] = useState<any>({ title: '', total_units: 1000, done_units: 0, minutes_per_unit: 5, review_ratio: 0.3, weekly_available_hours: 20, rest_days_per_week: 1, deadline: '' })
  const [res, setRes] = useState<any>(null)
  if (!st) return null
  const subjects = Object.values<any>(st.c.subjects ?? {})
  const fill = (id: string) => { const s = subjects.find((x) => x.id === id); if (s) setF({ ...f, title: s.title, total_units: s.total_units || 0, done_units: s.done_units || 0, minutes_per_unit: s.minutes_per_unit || 5, deadline: s.exam_date || '' }) }
  const run = async () => { const r = await act(() => api('/api/simulate', { method: 'POST', body: f })); if (r) setRes(r) }
  const num = (k: string, label: string, step = 1) => <label className="text-xs font-semibold">{label}<input type="number" step={step} min={0} className={input} value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></label>
  return (
    <div className="space-y-3">
      <Card title="목표 설정">
        {subjects.length > 0 && <select className={input + ' mb-2'} onChange={(e) => fill(e.target.value)} defaultValue=""><option value="">등록된 학습 과목에서 불러오기…</option>{subjects.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}</select>}
        <div className="grid grid-cols-2 gap-2">
          <label className="text-xs font-semibold col-span-2">목표 이름<input className={input} value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} placeholder="예: 임용 1차 대비" /></label>
          {num('total_units', '전체 분량')}{num('done_units', '이미 한 분량')}{num('minutes_per_unit', '분량 1단위당 소요(분)')}{num('review_ratio', '복습 비율 (0.3 = 30% 추가)', 0.1)}
          {num('weekly_available_hours', '주간 확보 가능 시간')}{num('rest_days_per_week', '주간 휴식일')}
          <label className="text-xs font-semibold col-span-2">목표일<input type="date" className={input} value={f.deadline} onChange={(e) => setF({ ...f, deadline: e.target.value })} /></label>
        </div>
        <div className="mt-2"><Btn kind="primary" onClick={run}>세 가지 계획 비교</Btn></div>
      </Card>
      {res && <>
        <Card title="시뮬레이션 결과"><p className="text-sm">남은 분량 {res.remaining_units} · 남은 기간 {res.days_left}일 · 필요한 총 시간 약 {res.total_hours_needed}시간 (복습 포함)</p>
          <p className="text-xs text-stone-500 mt-1">⚠ {res.disclaimer}</p></Card>
        <div className="grid gap-3 sm:grid-cols-3">{res.plans.map((p: any) => (
          <Card key={p.name} title={p.name} right={<Badge cls={p.burden.startsWith('불가능') ? 'bg-red-200 text-red-900' : p.burden === '높음' ? 'bg-coral' : p.burden === '보통' ? 'bg-sun' : 'bg-mint'}>부담 {p.burden.split(' ')[0]}</Badge>}>
            <p className="text-xs text-stone-500 mb-1">{p.description}</p>
            <ul className="text-sm space-y-0.5"><li>주당 <b>{p.weekly_hours}시간</b> (하루 {p.daily_hours}시간 × 주 {p.study_days_per_week}일)</li><li>주당 분량 {p.units_per_week}</li><li>완료 예정 {p.finish_date} (여유 {p.buffer_days}일)</li></ul>
            <div className="mt-2 text-xs">확보 시간 대비 {p.load_pct ?? '∞'}%<Bar pct={p.load_pct ?? 100} color={p.load_pct > 100 ? '#e5484d' : p.load_pct > 85 ? '#f6a192' : '#8fd3b6'} /></div>
          </Card>))}</div></>}
    </div>
  )
}

// ------------------------------------------------------------------ 비상대책실
export function Emergency() {
  const refresh = useRefresh()
  const { setPage } = useUI()
  const { data: st } = useLife()
  const { data, refetch } = useQuery({ queryKey: ['emergency', st?.rev], queryFn: () => api('/api/emergency') })
  const pending = st?.approvals.filter((a: any) => a.status === 'PENDING') ?? []
  const doIt = async (action: string) => {
    if (action === 'settings' || action === 'snapshots') return setPage('settings')
    if (action === 'export') return act(() => download('/api/export', 'lifeos-export.json'))
    const r = await act(() => api('/api/emergency/action', { method: 'POST', body: { action } }))
    if (r) useUI.getState().notify(r.message)
    refresh(); refetch()
  }
  const inc = data?.incidents ?? []
  return (
    <div className="space-y-3">
      <Card title="긴급 상황 목록" right={<Badge cls={inc.length ? 'bg-coral' : 'bg-mint'}>{inc.length ? `${inc.length}건` : '이상 없음'}</Badge>}>
        {inc.length === 0 && <Empty>지금 대응이 필요한 상황이 없습니다.</Empty>}
        <div className="space-y-2">{inc.map((i: any) => (
          <div key={i.id} className="border-2 border-brown rounded-md p-2 bg-cream">
            <div className="flex items-center gap-2"><Badge cls={i.level === 'HIGH' ? 'bg-red-200 text-red-900' : 'bg-sun'}>{i.level === 'HIGH' ? '긴급' : '주의'}</Badge><b className="text-sm">{i.title}</b></div>
            <p className="text-sm mt-1"><span className="text-stone-500">원인: </span>{i.cause}</p>
            <div className="flex flex-wrap gap-1.5 mt-2">{i.options.map((o: any) => <Btn key={o.action} small onClick={() => doIt(o.action)}>{o.label}</Btn>)}</div>
          </div>))}</div>
      </Card>
      <Card title="승인 요청"><p className="text-sm">{pending.length ? `대기 중인 승인 요청 ${pending.length}건 — 업무 센터에서 처리하세요.` : '대기 중인 승인 요청이 없습니다.'}</p>
        <p className="text-xs text-stone-500 mt-1">데이터를 지우거나 일정을 바꾸는 복구 조치는 항상 승인을 거친 뒤에만 적용됩니다.</p></Card>
    </div>
  )
}

// ------------------------------------------------------------------ 시스템 설정
function AIControl() {
  const { ai, setAI } = useUI()
  const [models, setModels] = useState<ModelInfo[] | null>(null)
  const supported = webgpuSupported()
  const find = async () => { try { setModels(await listModels()) } catch (e: any) { setAI({ status: 'error', text: 'AI 라이브러리를 불러오지 못했습니다: ' + (e?.message ?? '') }) } }
  const start = async (id: string, vram = 0) => {
    if (!confirm(`모델 "${id}"을(를) 내려받아 이 기기에서 실행합니다.\n용량이 수백 MB 이상일 수 있어 Wi-Fi 사용을 권장합니다. 계속할까요?`)) return
    setAI({ status: 'loading', progress: 0, text: '준비 중…', model: id })
    try {
      await loadModel(id, (text, progress) => setAI({ text, progress }), vram)
      localStorage.setItem('lifeos.aiModel', id)
      setAI({ status: 'ready', text: '', progress: 100 })
    } catch (e: any) { setAI({ status: 'error', text: '모델 실행 실패: ' + (e?.message ?? e) }) }
  }
  return (
    <Card title="로컬 AI 설정" right={<Badge cls={ai.status === 'ready' ? 'bg-mint' : 'bg-stone-200'}>{{ off: '꺼짐', unsupported: '미지원', loading: '불러오는 중', ready: '켜짐', error: '오류' }[ai.status]}</Badge>}>
      <p className="text-sm">{deviceHint()}</p>
      <p className="text-xs text-stone-500 mt-1">AI가 꺼져 있어도 일정·학습·재무 등 규칙 기반 업무는 모두 동작합니다. AI는 규칙으로 해석할 수 없는 자유 지시와 회의 토론에만 쓰이며, 직원 12명이 모델 하나를 순서대로 공유합니다. 추론은 이 기기 안에서만 실행됩니다.</p>
      {ai.status === 'loading' && <div className="mt-2"><Bar pct={ai.progress} /><p className="text-xs mt-1 break-all">{ai.text}</p></div>}
      {ai.status === 'error' && <p className="text-sm text-red-800 bg-red-100 rounded p-2 mt-2">{ai.text}</p>}
      {ai.status === 'ready' && <div className="mt-2 flex items-center gap-2 text-sm"><span>실행 중: <b className="break-all">{ai.model}</b></span><Btn small onClick={async () => { await unloadModel(); setAI({ status: 'off' }) }}>끄기</Btn></div>}
      {supported && ai.status !== 'ready' && ai.status !== 'loading' && <div className="mt-2">
        {!models ? <Btn onClick={find}>사용 가능한 모델 찾기</Btn> : models.length === 0 ? <Empty>실행 가능한 작은 모델을 찾지 못했습니다.</Empty> :
          <ul className="space-y-1">{models.map((m) => <li key={m.id} className="flex items-center justify-between gap-2 text-sm border-b border-brown/10 py-1">
            <span className="min-w-0"><span className="break-all">{m.id}</span> <span className="text-xs text-stone-500">필요 메모리 약 {m.vram}MB{m.low ? ' · 저사양 가능' : ''}{/qwen|gemma/i.test(m.id) ? ' · 한국어 권장' : ''}{m.vram <= 1500 ? ' · 경량 모델로 사용' : ' · 상위 모델로 사용'}{m.id === ai.model ? ' · 지난번 사용' : ''}</span></span>
            <Btn small kind="primary" onClick={() => start(m.id, m.vram)}>켜기</Btn></li>)}</ul>}
      </div>}
    </Card>
  )
}

function OllamaControl() {
  const { setAI } = useUI()
  const [c, setC] = useState<OllamaCfg>(ollamaCfg())
  const [models, setModels] = useState<{ name: string; size: number }[] | null>(null)
  const [err, setErr] = useState('')
  const connect = async () => {
    setErr('')
    try { const m = await ollamaModels(c.url); setModels(m); const n = { ...c, on: true }; setC(n); saveOllama(n); setAI({ status: 'ready' }) }
    catch (e: any) { setOllamaReady(false); setErr(`연결 실패: ${e?.message ?? e}. Ollama가 켜져 있는지, 환경변수 OLLAMA_ORIGINS에 이 사이트 주소(${location.origin})를 넣었는지 확인하세요.`) }
  }
  const upd = (p: Partial<OllamaCfg>) => { const n = { ...c, ...p }; setC(n); saveOllama(n); setAI({ status: 'ready' }) }
  const off = () => { const n = { ...c, on: false }; setC(n); saveOllama(n); setOllamaReady(false); setModels(null); setAI({ status: 'off' }) }
  const opts = models ?? []
  return (
    <Card title="Ollama 연결 (내 컴퓨터의 로컬 모델, 선택)" right={<Badge cls={c.on && models ? 'bg-mint' : 'bg-stone-200'}>{c.on && models ? '연결됨' : '꺼짐'}</Badge>}>
      <p className="text-xs text-stone-500">컴퓨터에 Ollama를 설치해 두면 이 브라우저가 그 모델을 직접 부릅니다(서버를 거치지 않음). Render 서버에서는 Ollama를 실행할 수 없습니다. 작업 유형에 따라 짧은 요약·분류는 경량 모델, 계획·분석·답변은 상위 모델로 보내고, 경량 모델의 검증 통과율이 70% 아래로 떨어지면 상위 모델로 바꿉니다.</p>
      <div className="flex gap-2 mt-2"><input className={input} value={c.url} onChange={(e) => setC({ ...c, url: e.target.value })} /><Btn onClick={connect}>연결</Btn>{c.on && <Btn onClick={off}>끊기</Btn>}</div>
      {err && <p className="text-xs text-red-800 mt-1">{err}</p>}
      {models && <div className="grid grid-cols-2 gap-2 mt-2">
        {(['light', 'strong'] as const).map((tier) => <label key={tier} className="text-xs font-semibold">{tier === 'light' ? '경량 모델 (분류·요약·태그)' : '상위 모델 (계획·분석·답변)'}
          <select className={input} value={c[tier]} onChange={(e) => upd({ [tier]: e.target.value } as any)}><option value="">사용 안 함</option>{opts.map((m) => <option key={m.name} value={m.name}>{m.name} ({(m.size / 1e9).toFixed(1)}GB)</option>)}</select></label>)}
      </div>}
      <p className="text-[11px] text-stone-500 mt-1">⚠ 이 연결은 개발 환경에서 실제 Ollama로 시험하지 못했습니다. 브라우저 정책에 따라 사설 네트워크 접근 허용 창이 뜨거나 차단될 수 있습니다.</p>
    </Card>
  )
}

export function Settings() {
  const { data: st } = useLife()
  const refresh = useRefresh()
  const qc = useQueryClient()
  const { data: twin } = useQuery({ queryKey: ['twin', st?.rev], queryFn: () => api('/api/twin') })
  const [snaps, setSnaps] = useState<string[] | null>(null)
  const [pw, setPw] = useState('')
  const [queued, setQueued] = useState(0)
  useEffect(() => { outboxAll().then((o) => setQueued(o.length)) }, [st?.rev])
  if (!st) return null
  const s = st.settings, u = st.usage.today, sync = st.sync
  const put = async (body: any) => { await act(() => api('/api/settings', { method: 'PUT', body }), '설정을 저장했습니다'); refresh() }
  const limit = (k: string, label: string, used?: number) => (
    <label className="block text-xs font-semibold mb-2">{label}{used !== undefined && <span className="font-normal text-stone-500"> — 사용 {used} / {s[k]}</span>}
      {used !== undefined && <Bar pct={(100 * used) / s[k]} color={used >= s[k] ? '#e5484d' : '#8fd3b6'} />}
      <input type="number" className={input + ' mt-1'} defaultValue={s[k]} key={s[k]} onBlur={(e) => Number(e.target.value) !== s[k] && put({ [k]: e.target.value })} /></label>)
  const restore = async (name: string) => {
    const password = prompt(`${name} 스냅샷으로 되돌리면 그 이후의 변경이 사라집니다.\n계속하려면 비밀번호를 입력하세요.`)
    if (!password) return
    if (await act(() => api('/api/snapshots/restore', { method: 'POST', body: { name, password } }), '스냅샷으로 복구했습니다')) refresh()
  }
  const changePw = async () => { const r = await act(() => api('/api/password', { method: 'POST', body: { password: pw } }), '비밀번호를 바꿨습니다. 다른 기기의 로그인은 모두 해제됩니다.'); if (r) { setCsrf(r.csrf); setPw('') } }
  const syncLabel: any = { SAVED: ['영구 저장 완료', 'bg-mint'], PENDING: ['저장 대기 중 (아직 영구 저장 전)', 'bg-sun'], ERROR: ['저장 실패', 'bg-red-200 text-red-900'], LOCAL_ONLY: ['영구 저장소 미연결', 'bg-coral'] }[sync.state]
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Card title="저장 상태 · 백업" right={<Badge cls={syncLabel[1]}>{syncLabel[0]}</Badge>}>
        <ul className="text-sm space-y-0.5">
          <li>· 저장소: {sync.backend}</li><li>· 마지막 영구 저장: {fmtTime(sync.last_sync)}</li><li>· 마지막 일일 스냅샷: {sync.last_snapshot ?? '없음'}</li>
          <li>· 다른 기기와의 충돌 병합: {sync.conflicts}회</li><li>· 이 기기에 보관 중인 미전송 변경: {queued}건</li>
          {sync.error && <li className="text-red-800">· {sync.error}</li>}
          {!sync.durable && <li className="text-red-800">· GitHub 저장소가 연결되지 않았습니다. 서버가 재시작되면 데이터가 사라집니다. README의 환경변수 설정을 확인하세요.</li>}
        </ul>
        <div className="flex flex-wrap gap-1.5 mt-2">
          <Btn small kind="primary" onClick={async () => { const r = await act(() => api('/api/sync', { method: 'POST' })); if (r) useUI.getState().notify(r.ok ? '영구 저장소에 저장했습니다' : r.sync.error || '저장하지 못했습니다', r.ok ? 'ok' : 'err'); refresh() }}>지금 저장</Btn>
          <Btn small onClick={() => act(() => download('/api/export', 'lifeos-export.json'))}>전체 데이터 내려받기</Btn>
          <Btn small onClick={async () => { const r = await act(() => api('/api/snapshots')); if (r) setSnaps(r.snapshots) }}>스냅샷 목록</Btn>
        </div>
        {snaps && (snaps.length === 0 ? <Empty>스냅샷이 아직 없습니다.</Empty> : <ul className="mt-2 text-sm">{snaps.slice(-10).reverse().map((n) => <li key={n} className="flex justify-between py-0.5 border-b border-brown/10"><span>{n}</span><Btn small kind="danger" onClick={() => restore(n)}>이 날짜로 복구</Btn></li>)}</ul>)}
      </Card>

      <Card title="무료 한도 관리">
        {st.session?.limit && <p className="text-sm bg-sun border border-brown/40 rounded p-1.5 mb-2">⚠ {st.session.limit}</p>}
        <p className="text-sm mb-2">이번 달 서버 가동 추정 <b>{st.usage.month_hours}시간</b> / Render 무료 750시간<Bar pct={(100 * st.usage.month_hours) / 750} /></p>
        <p className="text-xs text-stone-500 mb-2">오늘 GitHub 커밋 {u.commits}회 · 정기 점검(Actions) 이번 달 {st.usage.actions_runs_month}회 기록 (1회 약 1분, 무료 2,000분/월). 한도에 닿으면 유료 자원을 켜지 않고 업무를 대기 상태로 둡니다.</p>
        {limit('max_steps_per_day', '하루 실행 단계 한도', u.steps)}
        {limit('max_tasks_per_session', '접속 1회당 작업 한도', st.session?.tasks_run ?? 0)}
        {limit('ai_daily_limit', '하루 로컬 AI 호출 한도', u.model_calls)}
        {limit('commits_per_day', '하루 GitHub 커밋 한도', u.commits)}
        {limit('render_hours_limit', '서버 가동 시간 보호선(시간/월)')}
      </Card>

      <AIControl />
      <OllamaControl />

      <Card title="웹 자료 조사 · 서버 예약 실행">
        <label className="text-sm flex items-center gap-2"><input type="checkbox" checked={!!s.web_research} onChange={(e) => put({ web_research: e.target.checked })} /> 위키백과 공개 API로 자료 조사 허용 (자료 조사원이 사용)</label>
        {limit('web_daily_limit', '하루 웹 조사 호출 한도', u.web_calls ?? 0)}
        <p className="text-xs text-stone-500">켜면 서버가 ko.wikipedia.org에 검색어만 보냅니다. 출처 주소와 가져온 시각을 함께 기록합니다. ⚠ 실제 위키백과 접속은 개발 환경에서 시험하지 못했고, 모의 응답으로만 검증했습니다.</p>
        <p className="text-sm mt-2">서버 예약 실행: {st.cron_enabled ? <b>켜짐</b> : <b className="text-stone-500">꺼짐 (Render 환경변수 CRON_TOKEN 필요)</b>}</p>
        <p className="text-xs text-stone-500">반복 업무 규칙에서 '서버 예약 실행'을 켠 규칙은 브라우저를 닫아도 실행됩니다. GitHub Actions가 정해진 시각에 서버를 깨워 실행시키며, 화면을 닫은 직후 서버가 깨어 있는 동안(약 15분)에도 실행됩니다. 로컬 AI가 필요한 단계와 승인이 필요한 단계는 기다립니다. 컴퓨터가 꺼져 있으면 로컬 AI는 실행되지 않습니다.</p>
        {limit('cron_max_steps', '예약 실행 1회당 최대 단계')}
        {st.ai_stats?.length > 0 && <details className="mt-2"><summary className="text-sm font-bold cursor-pointer">모델별 성능 기록</summary>
          <table className="w-full text-xs mt-1"><tbody>{st.ai_stats.map((x: any) => <tr key={x.id} className="border-b border-brown/10"><td className="py-0.5 pr-2 break-all">{x.model}</td><td className="pr-2">{x.task_type}</td><td className="pr-2">{x.ok}/{x.n} 통과</td><td>{x.n ? Math.round(x.ms / x.n) : 0}ms</td></tr>)}</tbody></table></details>}
      </Card>

      <Card title="자동화 설정" right={<Btn small kind={s.paused ? 'primary' : 'default'} onClick={() => put({ paused: !s.paused })}>{s.paused ? '자동화 재개' : '자동화 일시중지'}</Btn>}>
        {limit('discovery_interval_sec', '새 업무 탐색 최소 간격(초)')}{limit('max_retries', '작업 실패 시 재시도 횟수')}{limit('daily_hour_limit', '하루 일정 과부하 기준(시간)')}
        <b className="text-sm">반복 업무 규칙</b><Crud name="rules" columns={['title', 'action', 'every_days', 'enabled']} />
        <p className="text-xs text-stone-500 mt-1">사이트를 닫은 동안에는 업무가 실행되지 않습니다. 다음 접속 때 주기가 지난 규칙을 한 번만 실행합니다.</p>
      </Card>

      <Card title="개인정보 보호">
        <ul className="text-sm space-y-0.5"><li>· 저장 데이터 암호화: {sync.encrypted ? <b>켜짐</b> : <b className="text-red-800">꺼짐 — Render 환경변수 DATA_ENCRYPTION_KEY를 설정하면 켜집니다</b>}</li>
          <li>· GitHub 토큰과 비밀번호 해시는 브라우저로 전송되지 않습니다.</li><li>· 로컬 AI 추론은 이 기기에서만 실행되며 외부 AI API로 데이터를 보내지 않습니다.</li></ul>
        <div className="flex gap-2 mt-2"><input type="password" className={input} placeholder="새 비밀번호 (8자 이상)" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" /><Btn onClick={changePw} disabled={pw.length < 8}>변경</Btn></div>
        <div className="mt-2"><Btn small onClick={async () => { await act(() => api('/api/logout', { method: 'POST' })); qc.clear(); location.reload() }}>로그아웃</Btn></div>
      </Card>

      {twin && <Card title="디지털 분신 (업무 선호 기록)">
        <ul className="text-sm space-y-0.5">
          <li>· {twin.approval.note}</li>
          <li>· 주로 접속하는 시간대: {twin.active_hours.join(', ') || '기록 부족'}</li>
          <li>· 자주 지시하는 직원: {twin.frequent_agents.map((a: any) => `${a.agent}(${a.count})`).join(', ') || '기록 부족'}</li>
          <li>· 자주 쓰는 내보내기 형식: {twin.preferred_formats.join(', ') || '기록 부족'}</li>
          <li>· 직원 평가 평균: {twin.ratings.map((r: any) => `${r.agent} ${r.avg}점(${r.count}회)`).join(', ') || '기록 부족'}</li>
          <li>· 목표 우선순위: {twin.goal_priority.map((g: any) => `${g.title}(${g.importance})`).join(' > ') || '목표 없음'}</li>
        </ul><p className="text-xs text-stone-500 mt-2">{twin.applied}</p></Card>}

      <Card title="실행 로그 (감사 기록)" className="md:col-span-2">
        <div className="max-h-64 overflow-y-auto"><ul className="text-xs space-y-0.5">{[...st.audit].reverse().map((a: any) => <li key={a.id}><span className="text-stone-500">{fmtTime(a.ts)}</span> <b>{a.action}</b> {a.detail}</li>)}</ul></div>
        <details className="mt-2"><summary className="text-sm font-bold cursor-pointer">데이터 변경 이력 ({st.history.length})</summary><Lines lines={[...st.history].reverse().slice(0, 60).map((h: any) => `${fmtTime(h.ts)} ${h.collection} ${h.op} ${h.record}`)} /></details>
      </Card>
    </div>
  )
}

// ------------------------------------------------------------------ 최초 설정 · 로그인
export function Auth({ me, onDone }: { me: any; onDone: (csrf: string, firstRun: boolean) => void }) {
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [code, setCode] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const setup = me.setup_needed
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setErr('')
    if (setup && pw !== pw2) return setErr('비밀번호가 서로 다릅니다.')
    setBusy(true)
    try {
      const r = await api(setup ? '/api/setup' : '/api/login', { method: 'POST', body: setup ? { password: pw, setup_code: code } : { password: pw } })
      onDone(r.csrf, setup)
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="min-h-screen flex items-center justify-center p-4 bg-cream">
      <form onSubmit={submit} className="w-full max-w-sm bg-paper border-2 border-brown rounded-xl shadow-px p-5 space-y-3">
        <div className="text-center"><img src="/icon.svg" alt="" className="w-14 h-14 mx-auto" style={{ imageRendering: 'pixelated' }} /><h1 className="text-xl font-extrabold text-ink mt-1">LIFE OS</h1><p className="text-sm text-stone-600">나를 위해 일하는 작은 AI 회사</p></div>
        {!me.store_loaded && <p className="text-sm bg-red-100 text-red-900 rounded p-2">저장소를 불러오지 못했습니다: {me.storage.error}</p>}
        {setup && <p className="text-sm bg-sun rounded p-2 border border-brown/40">처음 오셨네요. 대표님 전용 비밀번호를 정하면 직원 12명을 배치합니다.</p>}
        <input type="password" className={input} placeholder={setup ? '비밀번호 (8자 이상)' : '비밀번호'} value={pw} onChange={(e) => setPw(e.target.value)} autoComplete={setup ? 'new-password' : 'current-password'} required minLength={setup ? 8 : 1} />
        {setup && <input type="password" className={input} placeholder="비밀번호 확인" value={pw2} onChange={(e) => setPw2(e.target.value)} autoComplete="new-password" required />}
        {setup && me.setup_code_required && <input className={input} placeholder="설정 코드 (Render 환경변수 SETUP_CODE 값)" value={code} onChange={(e) => setCode(e.target.value)} required />}
        {err && <p className="text-sm text-red-800">{err}</p>}
        <Btn kind="primary" type="submit" disabled={busy}>{busy ? '확인 중…' : setup ? '회사 설립하기' : '출근시키기'}</Btn>
        {setup && <ul className="text-xs text-stone-500 space-y-0.5"><li>· 저장소: {me.storage.backend} {me.storage.durable ? '(영구 저장)' : '— ⚠ 영구 저장소 미연결'}</li><li>· 데이터 암호화: {me.storage.encrypted ? '켜짐' : '꺼짐'}</li>
          <li>· 로컬 AI: {webgpuSupported() ? '이 기기에서 사용 가능 (설정에서 켤 수 있음)' : '이 기기는 미지원 → 규칙 기반 모드'}</li>{me.weak_secret && <li className="text-red-800">· ⚠ SECRET_KEY가 설정되지 않았습니다 (개발용 기본값 사용 중)</li>}</ul>}
      </form>
    </div>
  )
}

export function Welcome({ onClose }: { onClose: () => void }) {
  const { setPage } = useUI()
  const refresh = useRefresh()
  const [goal, setGoal] = useState('')
  const [area, setArea] = useState('학습')
  const save = async () => {
    if (goal.trim()) await act(() => api('/api/c/goals', { method: 'POST', body: { title: goal, area, importance: 5, status: '진행' } }), '목표를 등록했습니다')
    refresh(); onClose(); setPage('office')
  }
  return (
    <div className="fixed inset-0 z-50 bg-ink/50 flex items-center justify-center p-4">
      <div className="bg-paper border-2 border-brown rounded-xl shadow-px p-5 max-w-md w-full space-y-3">
        <h2 className="font-extrabold text-lg text-ink">직원 12명이 배치됐습니다 🎉</h2>
        <p className="text-sm">가장 중요한 목표를 하나 알려 주세요. 직원들은 대표님이 등록한 일정·목표·기록에서만 할 일을 찾고, 없는 목표를 지어내지 않습니다.</p>
        <input className={input} placeholder="예: 임용시험 합격" value={goal} onChange={(e) => setGoal(e.target.value)} maxLength={200} />
        <select className={input} value={area} onChange={(e) => setArea(e.target.value)}>{['학습', '집필', '재정', '프로젝트', '생활', '기타'].map((a) => <option key={a}>{a}</option>)}</select>
        <p className="text-xs text-stone-500">기본 업무 규칙: 조회·계산·요약은 자동 실행, 일정 변경은 승인 후 실행, 삭제는 항상 승인 필요. 직원별 권한은 직원 패널에서 바꿀 수 있습니다.</p>
        <div className="flex justify-end gap-2"><Btn onClick={save}>{goal.trim() ? '등록하고 사무실로' : '건너뛰고 사무실로'}</Btn></div>
      </div>
    </div>
  )
}
