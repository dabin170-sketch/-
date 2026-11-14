import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, download } from '../api'
import { act, useLife, useRefresh, useUI } from '../store'
import { Badge, Btn, Card, Empty, Lines, fmtTime, input } from '../ui'

const IN_STATUS: Record<string, [string, string]> = {
  queued: ['분석 대기', 'bg-sky'], processing: ['분석 중', 'bg-sky'], classified: ['분류 완료', 'bg-mint'],
  needs_review: ['확인 필요', 'bg-sun'], error: ['오류', 'bg-red-200 text-red-900'], unsupported: ['미지원', 'bg-stone-200'],
}
const CAND: Record<string, string> = { create_event: '일정 추가', create_project_task: '프로젝트 할 일 추가', set_exam_date: '시험일 등록' }
const C_STATUS: Record<string, [string, string]> = {
  pending: ['승인 대기', 'bg-coral'], needs_review: ['확인 필요', 'bg-sun'], applied: ['반영됨', 'bg-mint'],
  rejected: ['거절', 'bg-stone-200'], duplicate: ['이미 있음', 'bg-stone-200'],
}

// ------------------------------------------------------------------ AI 수집함
export function Intake() {
  const refresh = useRefresh()
  const { data: st } = useLife()
  const { data, refetch } = useQuery({ queryKey: ['intake', st?.rev], queryFn: () => api('/api/intake/items') })
  const [text, setText] = useState('')
  const [url, setUrl] = useState('')
  const [drag, setDrag] = useState(false)
  const file = useRef<HTMLInputElement>(null)
  const [filter, setFilter] = useState('')
  const send = async (fd: FormData) => {
    const r = await act(() => api('/api/intake/items', { method: 'POST', body: fd }))
    if (r) useUI.getState().notify(r.duplicate ? '이미 받은 내용이라 새로 만들지 않았습니다' : '수집함에 넣었습니다. 곧 분석합니다')
    refresh(); refetch()
  }
  const sendFiles = async (files: FileList | null) => { for (const f of Array.from(files ?? [])) { const fd = new FormData(); fd.append('file', f); await send(fd) } if (file.current) file.current.value = '' }
  const sendText = async () => { if (!text.trim()) return; const fd = new FormData(); fd.append('text', text); await send(fd); setText('') }
  const sendUrl = async () => { if (!url.trim()) return; const fd = new FormData(); fd.append('url', url); await send(fd); setUrl('') }
  const items: any[] = (data?.items ?? []).filter((i: any) => !filter || i.processing_status === filter)
  return (
    <div className="space-y-3">
      <Card title="한곳에 넣기">
        <div onDragOver={(e) => { e.preventDefault(); setDrag(true) }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); sendFiles(e.dataTransfer.files) }}
          className={`border-2 border-dashed rounded-lg p-4 text-center text-sm ${drag ? 'border-coral bg-coral/20' : 'border-brown/50 bg-cream'}`}>
          파일을 여기로 끌어다 놓거나 <Btn small onClick={() => file.current?.click()}>파일 고르기</Btn>
          <input ref={file} type="file" multiple className="hidden" accept=".txt,.md,.csv,.pdf,.docx,.png,.jpg,.jpeg,.gif,.webp,.mp3,.wav,.m4a,.ogg" onChange={(e) => sendFiles(e.target.files)} />
          <p className="text-xs text-stone-500 mt-1">읽을 수 있음: PDF(글자가 있는 것)·Word(.docx)·텍스트(.txt .md .csv), 3MB 이하 · 이미지와 음성은 받아 두지만 글자를 읽지 못합니다(무료 서버에 OCR·음성 인식기 없음)</p>
        </div>
        <textarea className={input + ' h-20 mt-2'} placeholder="공지·메모·카톡 내용을 그대로 붙여 넣어도 됩니다" value={text} onChange={(e) => setText(e.target.value)} />
        <div className="flex gap-2 mt-1"><Btn kind="primary" onClick={sendText} disabled={!text.trim()}>글 넣기</Btn></div>
        <div className="flex gap-2 mt-2"><input className={input} placeholder="https:// 링크 보관 (본문은 가져오지 않음)" value={url} onChange={(e) => setUrl(e.target.value)} /><Btn onClick={sendUrl}>링크 넣기</Btn></div>
      </Card>
      <div className="flex gap-1 flex-wrap text-xs">
        <button onClick={() => setFilter('')}><Badge cls={!filter ? 'bg-mint' : ''}>전체 {data?.items?.length ?? 0}</Badge></button>
        {Object.entries(IN_STATUS).map(([k, [l, c]]) => <button key={k} onClick={() => setFilter(k)}><Badge cls={filter === k ? c : ''}>{l} {st?.intake_summary?.[k] ?? 0}</Badge></button>)}
      </div>
      {items.length === 0 && <Empty>수집함이 비어 있습니다.</Empty>}
      {items.map((it) => <IntakeCard key={it.id} it={it} onChange={() => { refresh(); refetch() }} />)}
    </div>
  )
}

function IntakeCard({ it, onChange }: { it: any; onChange: () => void }) {
  const [edit, setEdit] = useState<Record<string, any>>({})
  const { data: st } = useLife()
  const decide = async (c: any, approve: boolean) => {
    const r = await act(() => api(`/api/intake/items/${it.id}/candidates/${c.id}`, { method: 'POST', body: { approve, payload: edit[c.id] } }), approve ? '반영하고 검수했습니다' : '거절했습니다')
    if (r) onChange()
  }
  const proj = it.related?.[0]
  return (
    <Card title={<span className="break-all">{it.title || it.original_filename}</span>} right={<Badge cls={IN_STATUS[it.processing_status]?.[1]}>{IN_STATUS[it.processing_status]?.[0]}</Badge>}>
      <p className="text-xs text-stone-500">{fmtTime(it.created_at)} · {it.source_type === 'file' ? it.original_filename : it.source_type === 'url' ? '링크' : '붙여 넣은 글'} · {it.kind}{it.upload_count > 1 ? ` · ${it.upload_count}번 받음(중복은 하나로)` : ''}</p>
      {it.error && <p className={`text-sm mt-1 rounded p-1.5 ${it.processing_status === 'error' ? 'bg-red-100 text-red-900' : 'bg-stone-100'}`}>{it.error}</p>}
      {it.classification && <p className="text-sm mt-1">분류: <b>{it.classification.category}</b> (신뢰도 {Math.round(it.classification.confidence * 100)}%) <span className="text-xs text-stone-500">{it.classification.reasons.join(', ')}</span></p>}
      {proj && <p className="text-sm">관련: {proj.type} 「{proj.title}」 <span className="text-xs text-stone-500">{proj.evidence}</span></p>}
      {it.extractions?.length > 0 && <details className="text-sm"><summary className="cursor-pointer">찾은 날짜 {it.extractions.length}개</summary>
        <Lines lines={it.extractions.map((x: any) => `${x.value}${x.time ? ' ' + x.time : ''} ${x.label ? `(${x.label})` : ''} — 신뢰도 ${Math.round(x.confidence * 100)}% · ${x.evidence_location}: ${x.snippet}`)} /></details>}
      {it.candidates?.length > 0 && <div className="mt-2 space-y-1.5">{it.candidates.map((c: any) => {
        const p = { ...c.proposed_payload, ...(edit[c.id] ?? {}) }
        const open = ['pending', 'needs_review', 'duplicate'].includes(c.approval_status)
        return (
          <div key={c.id} className="border border-brown/40 rounded p-2 bg-cream text-sm">
            <div className="flex flex-wrap gap-1.5 items-center"><Badge cls={C_STATUS[c.approval_status]?.[1]}>{C_STATUS[c.approval_status]?.[0]}</Badge><b>{CAND[c.action_type]}</b>
              <span className="text-xs text-stone-500">신뢰도 {Math.round(c.confidence * 100)}%</span></div>
            <div className="text-xs text-stone-600 mt-0.5">근거: {c.evidence}</div>
            {open ? <div className="grid grid-cols-2 gap-1.5 mt-1">
              {'title' in p && <input className={input + ' col-span-2'} value={p.title} onChange={(e) => setEdit({ ...edit, [c.id]: { ...(edit[c.id] ?? {}), title: e.target.value } })} />}
              {['date', 'due', 'exam_date'].filter((k) => k in p).map((k) => <input key={k} type="date" className={input} value={p[k] || ''} onChange={(e) => setEdit({ ...edit, [c.id]: { ...(edit[c.id] ?? {}), [k]: e.target.value } })} />)}
              {'start' in p && <input type="time" className={input} value={p.start || ''} onChange={(e) => setEdit({ ...edit, [c.id]: { ...(edit[c.id] ?? {}), start: e.target.value } })} />}
              {'project_id' in p && <span className="text-xs col-span-2">프로젝트: {st?.c?.projects?.[p.project_id]?.title ?? '(삭제됨)'}</span>}
              <div className="col-span-2 flex gap-2"><Btn small kind="primary" onClick={() => decide(c, true)}>{c.approval_status === 'duplicate' ? '그래도 반영' : '승인하고 반영'}</Btn><Btn small onClick={() => decide(c, false)}>거절</Btn></div>
            </div> : <div className="text-xs mt-1">{c.applied_record && `→ ${c.applied_record}`}{c.verification && ` · 검수 ${c.verification === 'passed' ? '통과' : '실패'}`}{c.duplicate_of && ' · 다른 수집 항목에 같은 후보가 있음'}</div>}
          </div>)
      })}</div>}
      {it.processing_status === 'error' && <div className="mt-1"><Btn small onClick={async () => { await act(() => api(`/api/intake/items/${it.id}/process`, { method: 'POST' })); onChange() }}>다시 분석</Btn></div>}
    </Card>
  )
}

// ------------------------------------------------------------------ AI가 발견한 일
export function Discoveries({ compact }: { compact?: boolean }) {
  const refresh = useRefresh()
  const { data: st } = useLife()
  const { setPage } = useUI()
  const { data, refetch } = useQuery({ queryKey: ['disc', st?.rev], queryFn: () => api('/api/discoveries') })
  const doIt = async (id: string, a: string, msg: string) => { if (await act(() => api(`/api/discoveries/${id}/${a}`, { method: 'POST' }), msg)) { refresh(); refetch() } }
  const list: any[] = data?.discoveries ?? []
  const shown = compact ? list.slice(0, 3) : list
  const name = (id: string) => st?.agents.find((a: any) => a.id === id)?.name ?? id
  if (compact && !list.length) return <Empty>지금 발견한 일이 없습니다.</Empty>
  return (
    <div className="space-y-2">
      {!compact && <p className="text-xs text-stone-500">규칙 기반 조건 검사로 찾은 일입니다(근거 표시). 「자동 처리」를 눌러도 일정·할 일을 바꾸는 단계는 계획을 보여 주고 다시 승인을 받습니다. 숨긴 제안 {data?.snoozed ?? 0}개 · 다시 제안하지 않는 종류 {data?.suppressed?.length ?? 0}개</p>}
      {!compact && !list.length && <Empty>지금 발견한 일이 없습니다.</Empty>}
      {shown.map((w) => {
        const d = w.discovery ?? {}
        return (
          <div key={w.id} className="border-2 border-brown/50 rounded-md p-2 bg-paper text-sm">
            <div className="flex flex-wrap gap-1.5 items-center"><b>{w.title}</b>
              <Badge cls={d.urgency === '높음' ? 'bg-coral' : d.urgency === '보통' ? 'bg-sun' : 'bg-cream'}>긴급 {d.urgency ?? '-'}</Badge><Badge>중요 {d.importance ?? '-'}</Badge></div>
            <Lines lines={w.evidence} />
            <p className="text-xs text-stone-500">담당 {name(d.agent ?? w.lead)} · 예상 {d.est_minutes ? `${d.est_minutes}분` : '-'} · 추천 실행일 {d.recommended_at ?? '-'} · 발견 규칙: {d.detection_rule ?? w.template}{w.project_title ? ` · 프로젝트 ${w.project_title}` : ''}</p>
            <div className="flex flex-wrap gap-1.5 mt-1">
              <Btn small kind="primary" onClick={() => doIt(w.id, 'auto', '자동 처리를 시작했습니다')}>자동 처리</Btn>
              <Btn small onClick={() => doIt(w.id, 'add_todo', '할 일에 추가했습니다')}>할 일에 추가</Btn>
              <Btn small onClick={() => doIt(w.id, 'snooze', '내일 다시 보여 드립니다')}>나중에</Btn>
              <Btn small onClick={() => doIt(w.id, 'dismiss', '무시했습니다')}>무시</Btn>
              <Btn small kind="ghost" onClick={() => doIt(w.id, 'never', '이 종류는 다시 제안하지 않습니다')}>다시 제안하지 않기</Btn>
            </div>
          </div>)
      })}
      {compact && list.length > 3 && <Btn small onClick={() => setPage('discover')}>모두 보기 ({list.length})</Btn>}
    </div>
  )
}

// ------------------------------------------------------------------ 검수 센터
const ART: Record<string, string> = { document: '문서', event: '일정', project_task: '할 일', research: '자료 조사', task: '규칙 업무', intake: '수집함', deletion: '삭제' }
export function VerifyCenter() {
  const { data: st } = useLife()
  const { openFlow } = useUI()
  const { data, refetch } = useQuery({ queryKey: ['verif', st?.rev], queryFn: () => api('/api/verifications') })
  const [type, setType] = useState('')
  const [fail, setFail] = useState(false)
  const runs: any[] = (data?.runs ?? []).filter((r: any) => (!type || r.artifact_type === type) && (!fail || r.status === 'failed'))
  const name = (id: string) => st?.agents.find((a: any) => a.id === id)?.name ?? id
  const recheck = async (r: any) => { const x = await act(() => api('/api/verifications', { method: 'POST', body: { artifact_type: r.artifact_type, artifact_id: r.artifact_id } })); if (x) { useUI.getState().notify(`재검수 ${x.status === 'passed' ? '통과' : '실패'}`, x.status === 'passed' ? 'ok' : 'err'); refetch() } }
  return (
    <div className="space-y-3">
      <Card title="검수 기록" right={<Badge cls={st?.verify_summary?.failed ? 'bg-coral' : 'bg-mint'}>실패 {st?.verify_summary?.failed ?? 0} / 전체 {st?.verify_summary?.total ?? 0}</Badge>}>
        <p className="text-xs text-stone-500 mb-2">직원이 '끝냈다'고 한 결과물을 실제로 다시 확인한 기록입니다. 검수에 실패한 단계는 완료로 넘어가지 않습니다. 문서·일정·할 일은 지금 상태로 다시 검수할 수 있습니다.</p>
        <div className="flex flex-wrap gap-1 text-xs mb-2">
          <button onClick={() => setType('')}><Badge cls={!type ? 'bg-mint' : ''}>전체</Badge></button>
          {Object.entries(ART).map(([k, l]) => <button key={k} onClick={() => setType(k)}><Badge cls={type === k ? 'bg-mint' : ''}>{l}</Badge></button>)}
          <label className="ml-2 inline-flex items-center gap-1"><input type="checkbox" checked={fail} onChange={(e) => setFail(e.target.checked)} />실패만</label>
        </div>
        {runs.length === 0 && <Empty>검수 기록이 없습니다.</Empty>}
        <div className="space-y-1.5">{runs.slice(0, 80).map((r) => (
          <div key={r.id} className={`border rounded p-2 text-sm ${r.status === 'failed' ? 'border-red-400 bg-red-50' : 'border-brown/30'}`}>
            <div className="flex flex-wrap gap-1.5 items-center"><Badge cls={r.status === 'passed' ? 'bg-mint' : 'bg-red-200 text-red-900'}>{r.status === 'passed' ? '통과' : '실패'}</Badge>
              <Badge>{ART[r.artifact_type] ?? r.artifact_type}</Badge><b className="break-all">{r.title || r.artifact_id}</b></div>
            <p className="text-xs text-stone-500">{fmtTime(r.completed_at)} · 담당 {name(r.agent_id)} · {r.source === 'recheck' ? '재검수' : r.source === 'rule_task' ? '규칙 업무' : '워크플로'} · 검수기 {r.verifier_version}</p>
            <ul className="text-xs mt-0.5">{r.checks.map((c: any, i: number) => <li key={i} className={c.passed ? '' : 'text-red-800'}>{c.passed ? '✔' : '✘'} {c.check_type} {c.passed ? (c.evidence && `— ${c.evidence}`) : `— ${c.error_message}`}</li>)}</ul>
            <div className="flex gap-2 mt-1">{['document', 'event', 'project_task'].includes(r.artifact_type) && r.artifact_id && <Btn small onClick={() => recheck(r)}>다시 검수</Btn>}
              {r.workflow_id && <Btn small kind="ghost" onClick={() => openFlow(r.workflow_id)}>워크플로 보기</Btn>}</div>
          </div>))}</div>
      </Card>
    </div>
  )
}

// ------------------------------------------------------------------ 내 인생 검색
const TYPES: Record<string, string> = { projects: '프로젝트', documents: '문서', events: '일정', project_tasks: '할 일', memories: 'AI 기억', meetings: '회의록',
  workflows: '실행 기록', writings: '창작 설정', subjects: '학습 과목', study_logs: '학습 기록', ideas: '아이디어', goals: '목표', files: '생성 결과물', intake: '수집함' }
export function LifeSearch() {
  const { data: st } = useLife()
  const { setPage, openFlow } = useUI()
  const refresh = useRefresh()
  const [q, setQ] = useState('')
  const [proj, setProj] = useState('')
  const [type, setType] = useState('')
  const [res, setRes] = useState<any>(null)
  const go = async () => { if (!q.trim()) return; const r = await act(() => api(`/api/personal-search?q=${encodeURIComponent(q)}&project_id=${proj}&types=${type}`)); if (r) setRes(r) }
  const ask = async () => { const r = await act(() => api('/api/workflows', { method: 'POST', body: { goal: q, project_id: proj || undefined } })); if (r) { refresh(); setPage('flows'); if (r.id) openFlow(r.id) } }
  const open = (h: any) => { if (h.url) return act(() => download(h.url, h.title)); if (h.type === 'workflows') { setPage('flows'); openFlow(h.id) } else setPage(h.location.page) }
  return (
    <div className="space-y-3">
      <Card title="한 번에 찾기">
        <div className="flex gap-2"><input className={input} placeholder="예: IEP, 발표, 영수증" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && go()} /><Btn kind="primary" onClick={go}>검색</Btn></div>
        <div className="grid grid-cols-2 gap-2 mt-2">
          <select className={input} value={proj} onChange={(e) => setProj(e.target.value)}><option value="">모든 프로젝트</option>{Object.values<any>(st?.c?.projects ?? {}).map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}</select>
          <select className={input} value={type} onChange={(e) => setType(e.target.value)}><option value="">모든 유형</option>{Object.entries(TYPES).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
        </div>
      </Card>
      {res && <Card title={`결과 ${res.count}건`} right={res.count > 0 ? <Btn small onClick={ask}>이 자료로 AI에게 묻기</Btn> : undefined}>
        <p className="text-xs text-stone-500 mb-2">{res.note}</p>
        {res.results.length === 0 ? <Empty>찾은 기록이 없습니다. 기록에 없는 내용은 추측해서 답하지 않습니다.</Empty> :
          <ul className="space-y-1.5">{res.results.map((h: any) => (
            <li key={h.type + h.id} className="border border-brown/30 rounded p-2 text-sm">
              <div className="flex flex-wrap gap-1.5 items-center"><Badge>{h.type_label}</Badge><b className="break-all">{h.title}</b>{h.project_title && <Badge cls="bg-sun">{h.project_title}</Badge>}<span className="text-xs text-stone-500">{h.date}</span></div>
              {h.snippet && <p className="text-xs text-stone-600 mt-0.5">…{h.snippet}…</p>}
              <div className="flex gap-2 mt-1 items-center"><Btn small onClick={() => open(h)}>{h.url ? '내려받기' : '열기'}</Btn><span className="text-[11px] text-stone-500">원본: {h.source} / {h.location.collection} / {h.id}</span></div>
            </li>))}</ul>}
      </Card>}
    </div>
  )
}
