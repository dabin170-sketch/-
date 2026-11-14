import { useRef, useState } from 'react'
import { api, download } from '../api'
import { Crud } from '../Crud'
import { act, useLife, useRefresh, useSchema, useUI } from '../store'
import { Badge, Bar, Btn, Card, Empty, Lines, fmtTime, input, taskLabel } from '../ui'

function Tabs({ tabs, value, onChange }: { tabs: [string, string][]; value: string; onChange: (v: string) => void }) {
  return <div className="flex gap-1 overflow-x-auto pb-1 mb-2">{tabs.map(([k, l]) =>
    <button key={k} onClick={() => onChange(k)} className={`shrink-0 px-2.5 py-1 text-sm rounded-md border-2 border-brown font-semibold ${value === k ? 'bg-mint shadow-pxs' : 'bg-paper'}`}>{l}</button>)}</div>
}

// ------------------------------------------------------------------ AI 회의실
export function Meetings() {
  const { data: st } = useLife()
  const refresh = useRefresh()
  const ai = useUI((s) => s.ai.status)
  const [title, setTitle] = useState('')
  const [type, setType] = useState('사용자 요청 회의')
  const [parts, setParts] = useState<string[]>(['schedule', 'study', 'life'])
  const [useAi, setUseAi] = useState(false)
  if (!st) return null
  const meetings = Object.values<any>(st.c.meetings ?? {}).sort((a, b) => b.created_at.localeCompare(a.created_at))
  const name = (id: string) => st.agents.find((a: any) => a.id === id)?.name ?? id
  const hold = async () => {
    if (!title.trim()) return
    if (await act(() => api('/api/meetings', { method: 'POST', body: { title, type, participants: parts, use_ai: useAi && ai === 'ready' } }), '회의를 진행했습니다')) { setTitle(''); refresh() }
  }
  const minutes = async (id: string) => { await act(() => api('/api/documents/generate', { method: 'POST', body: { doc_type: 'minutes', meeting_id: id } }), '회의록 문서 작성을 문서 제작자에게 맡겼습니다'); refresh() }
  return (
    <div className="space-y-3">
      <Card title="회의 열기">
        <div className="space-y-2">
          <input className={input} placeholder="회의 주제 (예: 이번 주 학습 시간을 어떻게 배분할 것인가?)" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
          <select className={input} value={type} onChange={(e) => setType(e.target.value)}>{['매일 아침 업무 회의', '주간 목표 회의', '프로젝트 점검 회의', '위기 대응 회의', '사용자 요청 회의'].map((t) => <option key={t}>{t}</option>)}</select>
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm">{st.agents.filter((a: any) => a.id !== 'chief').map((a: any) =>
            <label key={a.id} className="inline-flex items-center gap-1"><input type="checkbox" checked={parts.includes(a.id)} onChange={(e) => setParts(e.target.checked ? [...parts, a.id] : parts.filter((p) => p !== a.id))} />{a.name}</label>)}</div>
          <label className="text-sm flex items-center gap-1"><input type="checkbox" disabled={ai !== 'ready'} checked={useAi && ai === 'ready'} onChange={(e) => setUseAi(e.target.checked)} />
            로컬 AI 토론 추가 {ai !== 'ready' && <span className="text-stone-500">(로컬 AI가 꺼져 있어 규칙 기반 협업 보고서로 진행됩니다)</span>}</label>
          <Btn kind="primary" onClick={hold} disabled={!title.trim() || parts.length === 0}>회의 시작</Btn>
        </div>
      </Card>
      {meetings.length === 0 && <Empty>아직 열린 회의가 없습니다.</Empty>}
      {meetings.map((m) => (
        <Card key={m.id} title={m.title} right={<Badge cls={m.mode_label.includes('AI 토론') ? 'bg-sky' : 'bg-cream'}>{m.mode_label}</Badge>}>
          <p className="text-xs text-stone-500 mb-2">{m.type} · {fmtTime(m.created_at)} · 참석 {m.participants.map(name).join(', ')} · 사용한 자료: {m.sources.join(', ')}</p>
          <div className="space-y-2">{m.transcript.map((t: any, i: number) => <div key={i} className="text-sm"><b>{name(t.agent)}</b> <Badge cls={t.mode === 'ai' ? 'bg-sky' : 'bg-cream'}>{t.mode === 'ai' ? '로컬 AI 생성' : '규칙 기반'}</Badge><Lines lines={t.lines} /></div>)}</div>
          <div className="mt-2 text-sm"><b>결정 사항</b><Lines lines={m.conclusions} /></div>
          {m.action_items.length > 0 && <div className="mt-1 text-sm"><b>후속 업무</b><Lines lines={m.action_items.map((a: any) => `${name(a.agent)}: ${a.title} (${taskLabel(st.tasks.find((t: any) => t.id === a.task_id)?.status ?? '정리됨')})`)} /></div>}
          {m.needs_approval?.length > 0 && <div className="mt-1 text-sm"><b>승인 필요 항목</b><Lines lines={m.needs_approval} /></div>}
          <div className="mt-2"><Btn small onClick={() => minutes(m.id)}>회의록 문서 만들기</Btn></div>
        </Card>
      ))}
    </div>
  )
}

// ------------------------------------------------------------------ 일정 및 목표
const pad = (n: number) => String(n).padStart(2, '0')
const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
const addDays = (s: string, n: number) => { const d = new Date(s + 'T00:00:00'); d.setDate(d.getDate() + n); return ymd(d) }
const tmin = (t?: string) => (t ? Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5)) : null)

function Clock({ events }: { events: any[] }) {
  const timed = events.filter((e) => e.start && e.end)
  const colors = ['#8fd3b6', '#f6a192', '#8fb8d3', '#ffd36e', '#c7b8ea', '#ffb7b2']
  const pt = (m: number, r: number) => { const a = (m / 1440) * 2 * Math.PI - Math.PI / 2; return [100 + r * Math.cos(a), 100 + r * Math.sin(a)] }
  return (
    <svg viewBox="0 0 200 200" className="w-full max-w-xs mx-auto" role="img" aria-label="원형 시간표">
      <circle cx="100" cy="100" r="90" fill="#fffdf6" stroke="#7a5a3c" strokeWidth="2" />
      {timed.map((e, i) => { const a = tmin(e.start)!, b = tmin(e.end)!; const [x1, y1] = pt(a, 88), [x2, y2] = pt(b, 88), [tx, ty] = pt((a + b) / 2, 56)
        return <g key={e.id}><path d={`M100 100 L${x1} ${y1} A88 88 0 ${b - a > 720 ? 1 : 0} 1 ${x2} ${y2} Z`} fill={colors[i % colors.length]} stroke="#7a5a3c" strokeWidth="0.7" />
          <text x={tx} y={ty} fontSize="7" textAnchor="middle" fill="#4a3a2a">{e.title.slice(0, 7)}</text></g> })}
      {[0, 3, 6, 9, 12, 15, 18, 21].map((h) => { const [x, y] = pt(h * 60, 97); return <text key={h} x={x} y={y + 2.5} fontSize="7" textAnchor="middle" fill="#7a5a3c" fontWeight="bold">{h}</text> })}
      <circle cx="100" cy="100" r="3" fill="#7a5a3c" />
    </svg>
  )
}

export function Planner() {
  const { data: st } = useLife()
  const [tab, setTab] = useState('day')
  const [date, setDate] = useState<string>('')
  if (!st) return null
  const sel = date || st.today
  const events: any[] = Object.values(st.c.events ?? {})
  const on = (d: string) => events.filter((e) => e.date === d).sort((a, b) => (a.start || '99').localeCompare(b.start || '99'))
  const first = new Date(sel.slice(0, 8) + '01T00:00:00')
  const gridStart = addDays(ymd(first), -first.getDay())
  const weekStart = addDays(sel, -new Date(sel + 'T00:00:00').getDay())
  return (
    <div className="space-y-3">
      <Tabs tabs={[['day', '일간'], ['week', '주간'], ['month', '월간'], ['year', '연간'], ['clock', '원형 시간표'], ['goals', '목표'], ['routines', '루틴']]} value={tab} onChange={setTab} />
      {['day', 'week', 'month', 'year', 'clock'].includes(tab) && (
        <div className="flex items-center gap-2 text-sm">
          <Btn small onClick={() => setDate(addDays(sel, tab === 'month' ? -30 : tab === 'year' ? -365 : tab === 'week' ? -7 : -1))}>◀</Btn>
          <input type="date" className={input + ' max-w-[10.5rem]'} value={sel} onChange={(e) => setDate(e.target.value)} />
          <Btn small onClick={() => setDate(addDays(sel, tab === 'month' ? 30 : tab === 'year' ? 365 : tab === 'week' ? 7 : 1))}>▶</Btn>
          <Btn small onClick={() => setDate(st.today)}>오늘</Btn>
        </div>
      )}
      {tab === 'day' && <Card title={`${sel} 일정`}><Crud name="events" filter={(r) => r.date === sel} defaults={{ date: sel }} columns={['start', 'end', 'title', 'type', 'done']} sort={(a, b) => (a.start || '99').localeCompare(b.start || '99')} /></Card>}
      {tab === 'clock' && <Card title={`${sel} 원형 시간표`}><Clock events={on(sel)} />{on(sel).filter((e) => e.start && e.end).length === 0 && <Empty>시작·종료 시간이 있는 일정이 없습니다.</Empty>}</Card>}
      {tab === 'week' && <div className="grid grid-cols-1 sm:grid-cols-7 gap-1.5">{Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)).map((d) =>
        <button key={d} onClick={() => { setDate(d); setTab('day') }} className={`text-left bg-paper border-2 border-brown rounded p-1.5 min-h-[5rem] ${d === st.today ? 'bg-sun' : ''}`}>
          <div className="text-xs font-bold">{d.slice(5)} ({'일월화수목금토'[new Date(d + 'T00:00:00').getDay()]})</div>
          {on(d).map((e) => <div key={e.id} className={`text-xs truncate ${e.done ? 'line-through text-stone-400' : ''}`}>{e.start || '·'} {e.title}</div>)}</button>)}</div>}
      {tab === 'month' && <div><div className="grid grid-cols-7 text-center text-xs font-bold text-stone-500">{'일월화수목금토'.split('').map((d) => <div key={d}>{d}</div>)}</div>
        <div className="grid grid-cols-7 gap-0.5">{Array.from({ length: 42 }, (_, i) => addDays(gridStart, i)).map((d) => { const es = on(d); const inMonth = d.slice(0, 7) === sel.slice(0, 7)
          return <button key={d} onClick={() => { setDate(d); setTab('day') }} className={`text-left border border-brown/40 rounded-sm p-0.5 min-h-[3.2rem] ${d === st.today ? 'bg-sun' : inMonth ? 'bg-paper' : 'bg-stone-100 text-stone-400'}`}>
            <div className="text-[11px] font-bold">{Number(d.slice(8))}</div>{es.slice(0, 2).map((e) => <div key={e.id} className="text-[9px] leading-tight truncate bg-mint/60 rounded-sm mb-px">{e.title}</div>)}{es.length > 2 && <div className="text-[9px] text-stone-500">+{es.length - 2}</div>}</button> })}</div></div>}
      {tab === 'year' && <div className="grid grid-cols-3 sm:grid-cols-4 gap-2">{Array.from({ length: 12 }, (_, m) => `${sel.slice(0, 4)}-${pad(m + 1)}`).map((ym) => { const es = events.filter((e) => e.date?.startsWith(ym))
        return <button key={ym} onClick={() => { setDate(ym + '-01'); setTab('month') }} className="bg-paper border-2 border-brown rounded p-2 text-left"><div className="font-bold text-sm">{Number(ym.slice(5))}월</div>
          <div className="text-xs text-stone-600">일정 {es.length}건</div><div className="text-xs text-stone-600">마감 {es.filter((e) => e.type === '마감').length}건</div></button> })}</div>}
      {tab === 'goals' && <Card title="목표 관리"><Crud name="goals" columns={['title', 'area', 'importance', 'deadline', 'status']}
        extra={(g) => (g.target ? <span className="text-xs">{Math.round((100 * (g.current || 0)) / g.target)}%</span> : null)} />
        <p className="text-xs text-stone-500 mt-2">목표의 영역과 중요도는 직원들이 업무 우선순위를 계산할 때 사용합니다. 직원이 새 목표를 만들지는 않습니다.</p></Card>}
      {tab === 'routines' && <><Card title="루틴"><Crud name="routines" /></Card><Card title="루틴 실행 기록"><Crud name="routine_logs" defaults={{ date: st.today, done: true }} /></Card></>}
    </div>
  )
}

// ------------------------------------------------------------------ 기록 (학습·집필·재무·투자·생활·아이디어)
export function Records() {
  const [tab, setTab] = useState('study')
  const { data: st } = useLife()
  if (!st) return null
  const td = { date: st.today }
  return (
    <div>
      <Tabs tabs={[['study', '학습'], ['writing', '집필'], ['finance', '가계부'], ['invest', '투자'], ['life', '생활'], ['ideas', '아이디어']]} value={tab} onChange={setTab} />
      <div className="space-y-3">
        {tab === 'study' && <><Card title="학습 과목 (학습 코치 아테나)"><Crud name="subjects" columns={['title', 'total_units', 'exam_date', 'review_interval_days']} /></Card><Card title="학습 기록"><Crud name="study_logs" defaults={td} columns={['date', 'subject_id', 'units', 'kind']} /></Card></>}
        {tab === 'writing' && <><Card title="집필 프로젝트 (집필 편집자 뮤즈)"><Crud name="writings" columns={['title', 'target_count', 'deadline', 'status']} /></Card><Card title="집필 기록"><Crud name="writing_logs" defaults={td} /></Card></>}
        {tab === 'finance' && <><Card title="가계부 (재무 관리자 페니)"><Crud name="expenses" defaults={td} columns={['date', 'kind', 'category', 'amount', 'memo']} /></Card><Card title="월 예산 (매달 같은 한도 적용)"><Crud name="budgets" /></Card></>}
        {tab === 'invest' && <><Card title="보유 종목 (투자 분석가 불리)"><Crud name="holdings" columns={['name', 'quantity', 'avg_price', 'current_price']} />
          <p className="text-xs text-stone-500 mt-2">실시간 시세·공시 데이터는 연결되어 있지 않습니다. 현재가는 직접 입력한 값만 사용하며, 매매·주문 기능은 없습니다.</p></Card><Card title="투자 일지"><Crud name="invest_notes" defaults={td} /></Card></>}
        {tab === 'life' && <Card title="수면·활동 기록 (생활 코치 포근)"><Crud name="life_logs" defaults={td} /></Card>}
        {tab === 'ideas' && <Card title="아이디어 (창작 기획자 스파크)"><Crud name="ideas" columns={['title', 'tags', 'deadline', 'status']} /></Card>}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ 프로젝트 센터
export function Projects() {
  const { data: st } = useLife()
  const [open, setOpen] = useState<string | null>(null)
  if (!st) return null
  const projects = Object.values<any>(st.c.projects ?? {})
  const pts = Object.values<any>(st.c.project_tasks ?? {})
  return (
    <div className="space-y-3">
      <Card title="프로젝트"><Crud name="projects" columns={['title', 'deadline', 'status']} extra={(p) => <Btn small onClick={() => setOpen(open === p.id ? null : p.id)}>{open === p.id ? '접기' : '하위 작업'}</Btn>} /></Card>
      {projects.filter((p) => !open || p.id === open).map((p) => { const ts = pts.filter((t) => t.project_id === p.id); const done = ts.filter((t) => t.done).length; const pct = ts.length ? Math.round((100 * done) / ts.length) : 0
        const docs = Object.values<any>(st.c.memories ?? {}).filter((m) => m.project_id === p.id)
        return <Card key={p.id} title={p.title} right={<Badge cls="bg-mint">{done}/{ts.length} · {pct}%</Badge>}>
          <Bar pct={pct} />
          <p className="text-xs text-stone-500 my-1">{p.goal && `목표: ${p.goal} · `}마감 {p.deadline || '미정'} · 담당 직원: 마일로(프로젝트 매니저) · 다음 마일스톤: {ts.filter((t) => t.milestone && !t.done && t.due).sort((a, b) => a.due.localeCompare(b.due))[0]?.title ?? '없음'}</p>
          {(open === p.id || projects.length === 1) && <><Crud name="project_tasks" filter={(t) => t.project_id === p.id} defaults={{ project_id: p.id }} columns={['title', 'due', 'done', 'milestone']} sort={(a, b) => String(a.due || '9').localeCompare(String(b.due || '9'))} />
            {docs.length > 0 && <div className="mt-2 text-sm"><b>프로젝트 기억</b><Lines lines={docs.map((m) => m.content)} /></div>}</>}
        </Card> })}
    </div>
  )
}

// ------------------------------------------------------------------ 문서 공장
export function Docs() {
  const { data: st } = useLife()
  const { data: schema } = useSchema()
  const refresh = useRefresh()
  const { openTask } = useUI()
  const file = useRef<HTMLInputElement>(null)
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<any[] | null>(null)
  if (!st || !schema) return null
  const gen = async (doc_type: string) => { await act(() => api('/api/documents/generate', { method: 'POST', body: { doc_type } }), '문서 제작자 페이지에게 맡겼습니다. 완료되면 아래 목록에 나타납니다.'); refresh() }
  const jobs = st.tasks.filter((t: any) => t.kind === 'docs.generate').sort((a: any, b: any) => b.created_at.localeCompare(a.created_at)).slice(0, 6)
  const upload = async (f?: File) => { if (!f) return; const fd = new FormData(); fd.append('file', f); if (await act(() => api('/api/upload', { method: 'POST', body: fd }), '자료를 올렸습니다')) refresh(); if (file.current) file.current.value = '' }
  const search = async () => { const r = await act(() => api('/api/search?q=' + encodeURIComponent(q))); if (r) setHits(r.hits) }
  return (
    <div className="space-y-3">
      <Card title="자동 문서 만들기">
        <div className="flex flex-wrap gap-1.5">{Object.entries<string>(schema.doc_types).filter(([k]) => k !== 'minutes').map(([k, l]) => <Btn key={k} small onClick={() => gen(k)}>{l}</Btn>)}</div>
        <p className="text-xs text-stone-500 mt-2">저장된 실제 데이터로 내용을 채웁니다. 회의록은 AI 회의실에서 만들 수 있습니다. 전날 완료한 업무가 있으면 일일 보고서는 직원이 스스로 작성합니다.</p>
        {jobs.length > 0 && <ul className="mt-2 text-sm space-y-0.5">{jobs.map((t: any) => <li key={t.id}><button onClick={() => openTask(t.id)} className="underline decoration-dotted">{t.title}</button> <Badge>{taskLabel(t.status)}</Badge></li>)}</ul>}
      </Card>
      <Card title="문서 목록" right={<><input ref={file} type="file" accept=".txt,.md,.csv" className="hidden" onChange={(e) => upload(e.target.files?.[0])} /><Btn small onClick={() => file.current?.click()}>자료 올리기</Btn></>}>
        <Crud name="documents" columns={['title', 'type', 'status', 'due_date']} sort={(a, b) => b.updated_at.localeCompare(a.updated_at)}
          extra={(d) => <span className="whitespace-nowrap">{Object.keys(schema.export_formats).map((f) => <button key={f} title={schema.export_formats[f]} className="text-xs font-bold underline ml-1.5 uppercase"
            onClick={() => act(() => download(`/api/documents/${d.id}/export?fmt=${f}`, `${d.title}.${f}`))}>{f}</button>)}</span>} />
        <p className="text-xs text-stone-500 mt-2">내보내기 형식: {Object.values<string>(schema.export_formats).join(' · ')}. PDF 직접 생성은 지원하지 않습니다 — HTML 파일을 열어 인쇄에서 “PDF로 저장”을 사용하세요. 올릴 수 있는 자료는 300KB 이하의 텍스트(.txt .md .csv)입니다.</p>
      </Card>
      <Card title="자료 검색 (자료 조사원 아카이)">
        <div className="flex gap-2"><input className={input} placeholder="문서·기억·아이디어에서 키워드 검색" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && search()} /><Btn onClick={search}>검색</Btn></div>
        {hits && (hits.length === 0 ? <Empty>검색 결과가 없습니다.</Empty> : <ul className="mt-2 text-sm space-y-1">{hits.map((h) => <li key={h.id}><Badge>{{ documents: '문서', memories: '기억', ideas: '아이디어' }[h.collection as string]}</Badge> <b>{h.title}</b><div className="text-xs text-stone-500">…{h.snippet}…</div></li>)}</ul>)}
        <p className="text-xs text-stone-500 mt-2">웹 검색 기능은 제공하지 않습니다 (무료로 안정적으로 쓸 수 있는 수집 경로가 구현되어 있지 않음).</p>
      </Card>
    </div>
  )
}

// ------------------------------------------------------------------ 기억 보관소
export function Memory() {
  const { data: st } = useLife()
  const [agent, setAgent] = useState('')
  const [scope, setScope] = useState('')
  const [q, setQ] = useState('')
  const [reveal, setReveal] = useState(false)
  if (!st) return null
  const filter = (m: any) => (!agent || m.agent_id === agent) && (!scope || m.scope === scope) && (!q || `${m.content} ${m.tags}`.toLowerCase().includes(q.toLowerCase())) && (reveal || m.sensitivity !== '민감')
  const hidden = Object.values<any>(st.c.memories ?? {}).filter((m) => m.sensitivity === '민감').length
  return (
    <div className="space-y-3">
      <Card title="기억 보관소">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-2">
          <select className={input} value={agent} onChange={(e) => setAgent(e.target.value)}><option value="">모든 직원</option><option value="all">공통</option>{st.agents.map((a: any) => <option key={a.id} value={a.id}>{a.name}</option>)}</select>
          <select className={input} value={scope} onChange={(e) => setScope(e.target.value)}><option value="">모든 범위</option>{['단기', '프로젝트', '장기'].map((s) => <option key={s}>{s}</option>)}</select>
          <input className={input + ' col-span-2'} placeholder="내용·태그 검색" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <Crud name="memories" filter={filter} columns={['content', 'agent_id', 'scope', 'source']} />
        {hidden > 0 && <label className="text-sm mt-2 block"><input type="checkbox" checked={reveal} onChange={(e) => setReveal(e.target.checked)} /> 민감한 기억 {hidden}건 표시</label>}
        <p className="text-xs text-stone-500 mt-2">기억은 직원의 로컬 AI 프롬프트에 자료로 들어가며, 승인을 거절한 기록은 자동으로 기억에 남습니다. 모든 기억은 여기서 열람·수정·삭제할 수 있습니다.
          {st.sync.encrypted ? ' 저장소에는 암호화되어 기록됩니다.' : ' ⚠ 데이터 암호화가 꺼져 있습니다 — 민감한 내용은 암호화를 켠 뒤 저장하세요 (설정 › 개인정보 보호).'}</p>
      </Card>
    </div>
  )
}
