import { useState } from 'react'
import { api } from './api'
import { act, useLife, useRefresh, useSchema, useUI } from './store'
import { Badge, Bar, Btn, Empty, Lines, Modal, STATUS, fmtTime, input, taskLabel } from './ui'
import { Command, Metrics } from './pages/Flows'

export function Stars({ task }: { task: any }) {
  const refresh = useRefresh()
  return (
    <span className="whitespace-nowrap" aria-label="업무 평가">
      {[1, 2, 3, 4, 5].map((n) => (
        <button key={n} className={`text-lg ${task.rating >= n ? 'text-amber-500' : 'text-stone-300'}`} aria-label={`${n}점`}
          onClick={async () => { await act(() => api(`/api/tasks/${task.id}/action`, { method: 'POST', body: { action: 'rate', rating: n } })); refresh() }}>★</button>
      ))}
    </span>
  )
}

export function ApprovalBox({ ap }: { ap: any }) {
  const refresh = useRefresh()
  const { openFlow } = useUI()
  const go = async (approve: boolean, choice?: number) => {
    await act(() => api(`/api/approvals/${ap.id}`, { method: 'POST', body: { approve, choice } }), choice !== undefined ? '선택을 반영했습니다' : approve ? '승인했습니다' : '거절했습니다')
    refresh()
  }
  return (
    <div className="border-2 border-brown rounded-md bg-coral/30 p-2 text-sm">
      <div className="flex items-center gap-2 mb-1 flex-wrap"><Badge cls={ap.risk_level === 'HIGH' ? 'bg-red-200 text-red-900' : 'bg-sun'}>위험도 {ap.risk_level}</Badge><b className="text-ink">{ap.action}</b>
        {ap.workflow_id && <button className="text-xs underline" onClick={() => openFlow(ap.workflow_id)}>계획 보기</button>}</div>
      <Lines lines={ap.detail} />
      {ap.kind === 'choice' ? (
        <div className="flex flex-col gap-1 mt-2">{ap.options.map((o: any, i: number) => <Btn key={i} small kind={o.value?.cancel ? 'danger' : i === 0 ? 'primary' : 'default'} onClick={() => go(true, i)}>{o.label}</Btn>)}</div>
      ) : <div className="flex gap-2 mt-2"><Btn small kind="primary" onClick={() => go(true)}>승인</Btn><Btn small kind="danger" onClick={() => go(false)}>거절</Btn></div>}
    </div>
  )
}

export function TaskModal() {
  const { taskId, openTask } = useUI()
  const { data: st } = useLife()
  const refresh = useRefresh()
  const t = st?.tasks.find((x: any) => x.id === taskId)
  if (!t) return null
  const agent = st.agents.find((a: any) => a.id === t.assigned_agent)
  const logs = st.logs.filter((l: any) => l.task_id === t.id)
  const ap = st.approvals.find((a: any) => a.task_id === t.id && a.status === 'PENDING')
  const doAct = async (action: string) => { await act(() => api(`/api/tasks/${t.id}/action`, { method: 'POST', body: { action } })); refresh() }
  return (
    <Modal title={t.title} onClose={() => openTask(null)} wide>
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap gap-1.5 items-center">
          <Badge cls="bg-mint">{taskLabel(t.status)}</Badge><Badge>{agent?.name} · {agent?.title}</Badge>
          <Badge cls="bg-sun">우선순위 {t.priority}점</Badge><Badge>{t.source.startsWith('auto') ? '자율 발견' : t.source === 'user' ? '사용자 지시' : t.source.startsWith('meeting') ? '회의 결정' : '반복 규칙'}</Badge>
          {t.result?.ai && <Badge cls="bg-sky">로컬 AI 생성</Badge>}
        </div>
        {t.blocked_reason && <p className="bg-stone-200 border border-brown/40 rounded p-2">⏸ {t.blocked_reason}</p>}
        {t.error && <p className="bg-red-100 border border-red-300 rounded p-2 text-red-900">{t.error}</p>}
        {ap && <ApprovalBox ap={ap} />}
        <div><b>이 업무를 만든 근거</b><Lines lines={t.evidence} /></div>
        <div>
          <b>진행 단계</b> <span className="text-xs text-stone-500">({t.checkpoint.step}/{t.steps.length} · 단계마다 체크포인트 저장)</span>
          <Bar pct={t.progress} />
          <ol className="mt-1 space-y-0.5">{t.steps.map((s: string, i: number) => <li key={i}>{i < t.checkpoint.step ? '✅' : i === t.checkpoint.step && t.status === 'RUNNING' ? '▶️' : '⬜'} {i + 1}. {s}</li>)}</ol>
        </div>
        {t.result && <div className="bg-cream border border-brown/40 rounded p-2"><b>결과: {t.result.summary}</b><Lines lines={t.result.lines} />
          {t.review && <p className="text-xs text-stone-500 mt-1">총괄 관리자 검수: {t.review.passed ? '통과' : '보류'} — {t.review.note}</p>}</div>}
        {t.status === 'DONE' && <div>이 결과를 평가: <Stars task={t} /></div>}
        {t.priority_factors?.length > 0 && <details><summary className="font-bold cursor-pointer">우선순위 산정 근거 ({t.priority}점)</summary>
          <table className="w-full mt-1"><tbody>{t.priority_factors.map((f: any) => <tr key={f.name} className="border-b border-brown/10"><td className="py-0.5 pr-2 whitespace-nowrap">{f.name}</td><td className="pr-2 font-bold">{f.points > 0 ? '+' : ''}{f.points}</td><td className="text-stone-600">{f.why}</td></tr>)}</tbody></table></details>}
        <details open={logs.length < 8}><summary className="font-bold cursor-pointer">작업 로그 ({logs.length})</summary>
          <ul className="mt-1 space-y-0.5 text-xs">{logs.map((l: any) => <li key={l.id}><span className="text-stone-500">{fmtTime(l.ts)}</span> [{l.event_type}] {l.message}</li>)}</ul></details>
        <div className="flex gap-2">
          {['NEW', 'ANALYZING', 'QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'BLOCKED'].includes(t.status) && <Btn small kind="danger" onClick={() => doAct('stop')}>중단</Btn>}
          {['FAILED', 'STOPPED', 'BLOCKED'].includes(t.status) && <Btn small kind="primary" onClick={() => doAct('retry')}>체크포인트부터 다시 시도</Btn>}
        </div>
      </div>
    </Modal>
  )
}

export function Instruct({ agentId }: { agentId: string }) {
  const [text, setText] = useState('')
  const refresh = useRefresh()
  const ai = useUI((s) => s.ai.status)
  const send = async () => {
    if (!text.trim()) return
    if (await act(() => api('/api/tasks', { method: 'POST', body: { agent_id: agentId, text, importance: 2 } }), '업무를 지시했습니다')) { setText(''); refresh() }
  }
  return (
    <div>
      <div className="flex gap-2">
        <input className={input} placeholder="예: 이번 달 예산 점검해줘" value={text} maxLength={1000} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && send()} />
        <Btn kind="primary" onClick={send}>지시</Btn>
      </div>
      <p className="text-xs text-stone-500 mt-1">키워드로 해석되는 지시는 규칙 기반 업무로 바로 실행됩니다. {ai === 'ready' ? '그 밖의 지시는 로컬 AI가 처리합니다.' : '그 밖의 지시는 로컬 AI가 꺼져 있으면 대기 상태가 됩니다.'}</p>
    </div>
  )
}

const DECOR = ['', '배지', '명패', '왕관', '화분', '트로피']

export function AgentPanel() {
  const { agentId, openAgent, openTask, live } = useUI()
  const { data: st } = useLife()
  const { data: schema } = useSchema()
  const refresh = useRefresh()
  const a = st?.agents.find((x: any) => x.id === agentId)
  if (!a) return null
  const status = live[a.id] ?? a.status
  const tasks = st.tasks.filter((t: any) => t.assigned_agent === a.id)
  const cur = st.tasks.find((t: any) => t.id === a.current_task)
  const curFlow = st.workflows.find((w: any) => w.id === a.current_task)
  const done = tasks.filter((t: any) => t.status === 'DONE').sort((x: any, y: any) => y.completed_at.localeCompare(x.completed_at))
  const logs = st.logs.filter((l: any) => l.agent_id === a.id).slice(-12).reverse()
  const mems = Object.values<any>(st.c.memories ?? {}).filter((m) => m.agent_id === a.id || m.agent_id === 'all')
  const upd = async (body: any, msg: string) => { await act(() => api(`/api/agents/${a.id}`, { method: 'POST', body }), msg); refresh() }
  const prevXp = [...(schema?.levels ?? [])].reverse().find((l: any) => l.xp <= a.experience)?.xp ?? 0
  return (
    <Modal title={`${a.name} · ${a.title}`} onClose={() => openAgent(null)} wide>
      <div className="space-y-3 text-sm">
        <div className="flex gap-3 items-start">
          <div className="w-14 h-14 rounded-md border-2 border-brown shrink-0 flex items-center justify-center text-2xl" style={{ background: a.color }}>{a.name[0]}</div>
          <div className="flex-1 min-w-0">
            <div className="flex flex-wrap gap-1.5 mb-1"><Badge cls={STATUS[status]?.cls}>{STATUS[status]?.label}</Badge><Badge cls="bg-sun">Lv.{a.level} {a.level_label}</Badge><Badge>신뢰도 {a.trust}</Badge></div>
            <p className="text-stone-600">{a.personality} · 전문: {a.specialty}</p>
            <div className="mt-1"><Bar pct={a.next_xp ? (100 * (a.experience - prevXp)) / (a.next_xp - prevXp) : 100} color="#ffd36e" />
              <p className="text-xs text-stone-500">경험치 {a.experience}{a.next_xp ? ` / 다음 단계 ${a.next_xp}` : ' (최고 단계)'} · 완료 {a.stats.done} · 실패 {a.stats.failed} · 검수 통과 {a.stats.review_pass}</p>
              <p className="text-xs text-stone-500">해금한 장식: {DECOR.slice(1, a.level).join(', ') || '없음'} — 경험치는 실제로 완료한 업무와 평가로만 오릅니다.</p></div>
          </div>
        </div>
        <div><b>현재 업무</b>{cur ? <button className="block w-full text-left bg-cream border border-brown/40 rounded p-2 mt-1" onClick={() => openTask(cur.id)}>
          {cur.title} <Badge>{taskLabel(cur.status)}</Badge><Bar pct={cur.progress} /></button>
          : curFlow ? <button className="block w-full text-left bg-cream border border-brown/40 rounded p-2 mt-1" onClick={() => useUI.getState().openFlow(curFlow.id)}>워크플로: {curFlow.title}</button>
          : <Empty>지금 맡은 업무가 없습니다.</Empty>}</div>
        <div><b>업무 맡기기</b><Command agentId={a.id} compact /><p className="text-xs text-stone-500 mt-1">계획→실행→검증 순서로 처리하는 워크플로로 등록됩니다.</p></div>
        <div><b>실제 성능</b><Metrics m={a.metrics} /></div>
        <div className="flex gap-2">
          {a.paused ? <Btn kind="primary" onClick={() => upd({ paused: false }, '업무를 재개합니다')}>작업 재개</Btn> : <Btn onClick={() => upd({ paused: true }, '일시정지했습니다')}>작업 일시정지</Btn>}
        </div>
        <details><summary className="font-bold cursor-pointer">권한 설정</summary>
          <p className="text-xs text-stone-500 my-1">권한은 레벨이 올라도 자동으로 늘지 않습니다. 여기서 직접 바꿀 때만 변경됩니다. 위험도 HIGH 작업은 설정과 무관하게 항상 승인을 요청합니다.</p>
          {(schema?.writable ?? []).map((w: string) => (
            <label key={w} className="mr-3 inline-flex items-center gap-1"><input type="checkbox" checked={a.permissions.write.includes(w)}
              onChange={(e) => upd({ write: e.target.checked ? [...a.permissions.write, w] : a.permissions.write.filter((x: string) => x !== w) }, '권한을 변경했습니다')} />
              {schema.collections[w]?.label ?? (w === 'tasks' ? '작업 기록' : w)} 쓰기</label>
          ))}
          <label className="block mt-1"><input type="checkbox" checked={!!a.permissions.auto_medium} onChange={(e) => upd({ auto_medium: e.target.checked }, '권한을 변경했습니다')} /> 위험도 MEDIUM 단계를 승인 없이 실행</label>
        </details>
        <details open><summary className="font-bold cursor-pointer">완료한 업무 ({done.length})</summary>
          {done.slice(0, 8).map((t: any) => <div key={t.id} className="border-b border-brown/10 py-1 flex justify-between gap-2 items-center">
            <button className="text-left min-w-0" onClick={() => openTask(t.id)}><span className="block truncate">{t.result?.summary || t.title}</span><span className="text-xs text-stone-500">{fmtTime(t.completed_at)}</span></button><Stars task={t} /></div>)}
        </details>
        <details><summary className="font-bold cursor-pointer">작업 로그</summary><ul className="text-xs space-y-0.5 mt-1">{logs.map((l: any) => <li key={l.id}><span className="text-stone-500">{fmtTime(l.ts)}</span> {l.message}</li>)}</ul></details>
        <details><summary className="font-bold cursor-pointer">기억 ({mems.length})</summary>
          {mems.length ? <Lines lines={mems.map((m) => (m.sensitivity === '민감' ? '🔒 민감한 기억 (기억 보관소에서 확인)' : m.content))} /> : <Empty>기억 보관소에서 이 직원의 기억을 추가할 수 있습니다.</Empty>}</details>
      </div>
    </Modal>
  )
}
