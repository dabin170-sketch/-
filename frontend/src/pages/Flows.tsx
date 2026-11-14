import { useState } from 'react'
import { api, download, newId } from '../api'
import { ApprovalBox } from '../Panels'
import { act, useLife, useRefresh, useSchema, useUI } from '../store'
import { Badge, Btn, Card, Empty, Lines, Modal, fmtTime, input } from '../ui'

export const WF_STATUS: Record<string, [string, string]> = {
  PROPOSED: ['제안', 'bg-sky'], RUNNING: ['실행 중', 'bg-mint'], WAITING_APPROVAL: ['승인 대기', 'bg-coral'],
  WAITING_INPUT: ['선택 대기', 'bg-coral'], WAITING_AI: ['AI 응답 대기', 'bg-sky'], BLOCKED: ['막힘', 'bg-stone-300'], PAUSED: ['일시 정지', 'bg-stone-200'],
  DONE: ['완료', 'bg-emerald-200 text-emerald-900'], FAILED: ['실패', 'bg-red-200 text-red-900'], STOPPED: ['중단', 'bg-stone-200'],
}
const STEP: Record<string, [string, string]> = {
  PENDING: ['대기', '⬜'], RUNNING: ['실행 중', '▶️'], DONE: ['완료', '✅'], SKIPPED: ['건너뜀', '⏭️'], FAILED: ['실패', '❌'],
  FAILED_ATTEMPT: ['오류·수정 중', '🔁'], WAITING_AI: ['AI 대기', '⏳'], WAITING_INPUT: ['선택 대기', '✋'],
}
const PHASES = ['GOAL', 'CONTEXT', 'PLAN', 'EXECUTE', 'OBSERVE', 'VERIFY', 'REVISE', 'COMPLETE']
const PHASE_KO: Record<string, string> = { GOAL: '목표', CONTEXT: '맥락', PLAN: '계획', EXECUTE: '실행', OBSERVE: '관찰', VERIFY: '검증', REVISE: '수정', COMPLETE: '완료' }

/** 대시보드·업무 실행 화면 위쪽의 '맡기기' 입력창 */
export function Command({ agentId, compact }: { agentId?: string; compact?: boolean }) {
  const [goal, setGoal] = useState('')
  const [proj, setProj] = useState('')
  const [busy, setBusy] = useState(false)
  const refresh = useRefresh()
  const { data: st } = useLife()
  const { setPage, openFlow } = useUI()
  const send = async () => {
    if (!goal.trim() || busy) return
    setBusy(true)
    const r = await act(() => api('/api/workflows', { method: 'POST', body: { goal, agent_id: agentId, project_id: proj || undefined }, headers: { 'Idempotency-Key': newId('wfreq') } }))
    setBusy(false)
    if (!r) return
    setGoal(''); refresh()
    if (r.type === 'workflow' || r.type === 'existing') {
      useUI.getState().notify(r.type === 'existing' ? '같은 명령이 이미 진행 중이라 그 작업을 보여 드립니다' : `「${r.title}」 계획을 세우고 실행합니다`)
      if (!compact) { setPage('flows'); openFlow(r.id) }
    } else if (r.type === 'task') useUI.getState().notify('규칙 기반 업무로 등록했습니다 (업무 센터)')
  }
  const projects = Object.values<any>(st?.c?.projects ?? {})
  return (
    <div>
      {!compact && projects.length > 0 && <select className={input + ' mb-1.5'} value={proj} onChange={(e) => setProj(e.target.value)}>
        <option value="">프로젝트 자동 판단 (명령에서 찾음)</option>{projects.map((p) => <option key={p.id} value={p.id}>프로젝트: {p.title}</option>)}</select>}
      <div className="flex gap-2">
        <input className={input} value={goal} maxLength={1000} onChange={(e) => setGoal(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder={compact ? '예: 이번 달 예산 점검해줘' : '예: 내일 오후 3시 팀 회의 1시간 잡아줘 · 이번 주 발표 준비해줘 · 주간 보고서 만들어줘'} />
        <Btn kind="primary" onClick={send} disabled={busy || !goal.trim()}>{busy ? '…' : '맡기기'}</Btn>
      </div>
      {!compact && <p className="text-xs text-stone-500 mt-1">명령을 해석해 단계별 계획을 세우고, 실제 도구를 실행한 뒤 결과를 검증합니다. 일정·할 일·문서를 바꾸는 단계는 실행 전에 계획을 보여 주고 승인을 받습니다.</p>}
    </div>
  )
}

function Proposals({ list }: { list: any[] }) {
  const refresh = useRefresh()
  const doIt = async (id: string, action: string) => { await act(() => api(`/api/workflows/${id}/action`, { method: 'POST', body: { action } }), action === 'accept' ? '제안을 수락했습니다' : '제안을 거절했습니다'); refresh() }
  if (!list.length) return null
  return (
    <Card title={`직원들의 제안 ${list.length}건`} right={<Badge cls="bg-sky">승인해야 실행</Badge>}>
      <div className="space-y-2">{list.map((w) => (
        <div key={w.id} className="border border-brown/40 rounded p-2 bg-cream text-sm">
          <b>{w.title}</b><Lines lines={w.evidence} />
          <div className="flex gap-2 mt-1"><Btn small kind="primary" onClick={() => doIt(w.id, 'accept')}>수락</Btn><Btn small onClick={() => doIt(w.id, 'dismiss')}>거절</Btn></div>
        </div>))}</div>
    </Card>
  )
}

export function Flows() {
  const { data: st } = useLife()
  const { openFlow } = useUI()
  if (!st) return null
  const wfs: any[] = st.workflows
  const name = (id: string) => st.agents.find((a: any) => a.id === id)?.name ?? id
  const props = wfs.filter((w) => w.status === 'PROPOSED')
  const rest = wfs.filter((w) => w.status !== 'PROPOSED')
  return (
    <div className="space-y-3">
      <Card title="무엇을 맡길까요?"><Command /></Card>
      <Proposals list={props} />
      <Card title="워크플로">
        {rest.length === 0 ? <Empty>아직 실행한 워크플로가 없습니다.</Empty> :
          <ul className="space-y-1.5">{rest.map((w) => {
            const done = w.plan.filter((x: any) => ['DONE', 'SKIPPED'].includes(x.status)).length
            return (
              <li key={w.id}><button onClick={() => openFlow(w.id)} className="w-full text-left border border-brown/40 rounded p-2 hover:bg-cream">
                <div className="flex items-center gap-2 flex-wrap"><Badge cls={WF_STATUS[w.status]?.[1]}>{WF_STATUS[w.status]?.[0]}</Badge><b className="text-sm">{w.title}</b></div>
                <div className="text-xs text-stone-500 mt-0.5">{fmtTime(w.created_at)} · {PHASE_KO[w.phase]} 단계 · {done}/{w.plan.length || '?'}단계 · 참여 {[...new Set(w.plan.map((x: any) => name(x.agent)))].join(', ') || name(w.lead)}</div>
                {w.result && <div className="text-xs mt-0.5">→ {w.result.summary}</div>}
                {w.error && <div className="text-xs text-red-800 mt-0.5">{w.error}</div>}
              </button></li>)
          })}</ul>}
      </Card>
      <ToolCatalog />
    </div>
  )
}

function ToolCatalog() {
  const { data: schema } = useSchema()
  if (!schema?.tools) return null
  return (
    <Card title={`직원이 쓰는 도구 ${schema.tools.length}개`}>
      <details><summary className="text-sm cursor-pointer">목록 보기</summary>
        <table className="w-full text-xs mt-2"><tbody>{schema.tools.map((t: any) => (
          <tr key={t.name} className="border-b border-brown/10 align-top"><td className="py-1 pr-2 font-mono whitespace-nowrap">{t.name}</td><td className="pr-2 whitespace-nowrap">{t.label}</td>
            <td className="pr-2"><Badge cls={t.risk === 'HIGH' ? 'bg-red-200 text-red-900' : t.risk === 'MEDIUM' ? 'bg-sun' : 'bg-cream'}>{t.risk}</Badge></td><td className="text-stone-600">{t.doc}</td></tr>))}</tbody></table>
      </details>
      <p className="text-xs text-stone-500 mt-2">모든 도구는 서버의 실제 함수입니다. 로컬 AI 모델은 도구를 직접 호출하지 않으며(작은 모델의 도구 호출은 믿을 수 없어 지원하지 않음), 'AI 문장 생성' 단계에서 문장만 씁니다.</p>
    </Card>
  )
}

function Grant({ b }: { b: any }) {
  const { data: st } = useLife()
  const refresh = useRefresh()
  const a = st.agents.find((x: any) => x.id === b.agent)
  const grant = async () => { await act(() => api(`/api/agents/${b.agent}`, { method: 'POST', body: { write: [...new Set([...a.permissions.write, ...b.collections])] } }), '권한을 허용했습니다'); refresh() }
  return <div className="bg-stone-200 border border-brown/40 rounded p-2 text-sm">⏸ {a?.name}에게 {b.collections.join(', ')} 쓰기 권한이 없어 멈췄습니다. <Btn small kind="primary" onClick={grant}>이 권한 허용</Btn></div>
}

export function FlowModal() {
  const { flowId, openFlow } = useUI()
  const { data: st } = useLife()
  const refresh = useRefresh()
  const [open, setOpen] = useState<string | null>(null)
  const w = st?.workflows.find((x: any) => x.id === flowId)
  if (!w) return null
  const name = (id: string) => st.agents.find((a: any) => a.id === id)?.name ?? id
  const aps = st.approvals.filter((a: any) => a.workflow_id === w.id && a.status === 'PENDING')
  const doAct = async (action: string, rating?: number) => { await act(() => api(`/api/workflows/${w.id}/action`, { method: 'POST', body: { action, rating } })); refresh() }
  const phaseIdx = PHASES.indexOf(w.phase)
  return (
    <Modal title={w.title} onClose={() => openFlow(null)} wide>
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap gap-1.5 items-center"><Badge cls={WF_STATUS[w.status]?.[1]}>{WF_STATUS[w.status]?.[0]}</Badge><Badge>{w.template}</Badge>
          {w.project_title && <Badge cls="bg-sun">프로젝트 {w.project_title}</Badge>}<span className="text-xs text-stone-500">목표: {w.goal}</span></div>
        <div className="flex flex-wrap gap-1 text-[11px]">{PHASES.map((p, i) => <span key={p} className={`px-1.5 py-0.5 rounded border border-brown/40 ${i === phaseIdx ? 'bg-mint font-bold' : i < phaseIdx || w.status === 'DONE' ? 'bg-cream' : 'bg-white text-stone-400'}`}>{PHASE_KO[p]}</span>)}</div>
        {aps.map((a: any) => <ApprovalBox key={a.id} ap={a} />)}
        {w.status === 'BLOCKED' && w.blocked?.kind === 'permission' && <Grant b={w.blocked} />}
        {w.recovery && <div className="bg-red-100 border border-red-300 rounded p-2"><b>실패 원인</b> [{w.recovery.cause}] {w.recovery.message}<div className="text-xs mt-1">복구 상태: {w.recovery.resume_hint} (완료된 단계: {w.recovery.completed_steps.join(', ') || '없음'})</div></div>}
        {w.result && <div className="bg-cream border border-brown/40 rounded p-2"><b>결과: {w.result.summary}</b><Lines lines={w.result.lines} />
          {w.result.files?.length > 0 && <div className="flex flex-wrap gap-1.5 mt-1">{w.result.files.map((f: any) => <Btn key={f.id} small kind="primary" onClick={() => act(() => download(f.url, f.name))}>⬇ {f.name}</Btn>)}</div>}
          {w.result.applied_rules?.length > 0 && <p className="text-xs mt-1">적용한 확정 규칙: {w.result.applied_rules.join(', ')}</p>}
          {w.result.skipped?.length > 0 && <p className="text-xs mt-1 text-stone-600">건너뛴 단계: {w.result.skipped.join(' / ')}</p>}
          <div className="mt-1">결과 평가: {[1, 2, 3, 4, 5].map((n) => <button key={n} aria-label={`${n}점`} className={`text-lg ${w.rating >= n ? 'text-amber-500' : 'text-stone-300'}`} onClick={() => doAct('rate', n)}>★</button>)}</div></div>}
        {w.context?.scope && <details><summary className="font-bold cursor-pointer">사용한 기억 ({w.context.memories?.length ?? 0}) · 범위 {w.context.scope}</summary>
          <Lines lines={(w.context.memories ?? []).map((m: any) => `[${m.kind}${m.rule ? '·규칙' : ''}] ${m.sensitive ? '🔒 민감한 기억' : m.content}`)} />
          {w.context.superseded?.length > 0 && <Lines lines={w.context.superseded.map((m: any) => `(사용 안 함) ${m.content} — ${m.reason}`)} />}</details>}
        {w.plan.length > 0 && <Dag plan={w.plan} name={name} />}
        <Revise w={w} />
        <div>
          <b>실행 계획 v{w.plan_version ?? 1} ({w.plan.length}단계)</b>
          <ol className="mt-1 space-y-1">{w.plan.map((x: any) => (
            <li key={x.id} className="border border-brown/30 rounded">
              <button className="w-full text-left px-2 py-1 flex gap-2 items-start" onClick={() => setOpen(open === x.id ? null : x.id)}>
                <span>{STEP[x.status]?.[1] ?? '·'}</span>
                <span className="flex-1 min-w-0"><b>{x.id}</b> {x.goal} <span className="text-xs text-stone-500">— {name(x.agent)} · {x.tool}{x.depends_on.length ? ` · 선행 ${x.depends_on.join(',')}` : ''}</span>
                  {x.verify && <span className={`block text-xs ${x.verify.passed ? 'text-emerald-700' : 'text-red-700'}`}>검증 {x.verify.passed ? '통과' : '실패'}: {x.verify.checks.map((c: any) => (c.passed ? '✔' : '✘') + c.name).join(' ')}</span>}</span>
                <span className="text-xs text-stone-500 shrink-0">{STEP[x.status]?.[0]}</span>
              </button>
              {open === x.id && <div className="px-2 pb-2 text-xs space-y-1 bg-cream/60">
                <div>예상 산출물: {x.expected_output || '-'} · 완료 기준: {x.done_criteria} · 실패 시: {{ retry: '다시 시도', skip: '건너뛰기', abort: '중단', ask: '사용자에게 묻기', revise: '앞 단계부터 수정' }[x.on_fail as string]} (최대 {x.max_retries}회, 제한 {x.timeout_sec}초)</div>
                {x.inputs_from?.length > 0 && <div>입력: {x.inputs_from.join(', ')} 단계의 실제 산출물 ({Object.entries(x.input_digest).filter(([k]) => !k.startsWith('$ctx')).map(([k, v]) => `${k}#${v}`).join(' ')})</div>}
                {x.observation && <div>관찰: {x.observation}</div>}
                {x.verify?.checks?.map((c: any) => <div key={c.name}>{c.passed ? '✔' : '✘'} {c.name} {c.detail && <span className="text-stone-500">— {c.detail}</span>}</div>)}
                {x.errors?.map((e: any, i: number) => <div key={i} className="text-red-800">오류 {e.attempt}회차 [{e.cause}] {e.msg}</div>)}
                {x.skip_reason && <div>건너뛴 이유: {x.skip_reason}</div>}
                {x.output?.mode && <div>생성 방식: {x.output.mode === 'ai' ? `로컬 AI (${x.output.model})` : x.output.model}</div>}
                {x.ms > 0 && <div>실행 시간 {x.ms}ms</div>}
              </div>}
            </li>))}</ol>
          {w.plan.length === 0 && <Empty>계획을 세우는 중입니다.</Empty>}
        </div>
        <details><summary className="font-bold cursor-pointer">실행 기록 ({w.trace.length})</summary>
          <ul className="mt-1 space-y-0.5 text-xs">{w.trace.map((t: any, i: number) => <li key={i}><span className="text-stone-500">{fmtTime(t.ts)}</span> <b>{PHASE_KO[t.phase]}</b>{t.step ? ` ${t.step}` : ''} — {t.msg}</li>)}</ul></details>
        <div className="flex gap-2">
          {['RUNNING', 'WAITING_APPROVAL', 'WAITING_INPUT', 'WAITING_AI', 'BLOCKED'].includes(w.status) && <Btn small onClick={() => doAct('pause')}>일시 정지</Btn>}
          {w.status === 'PAUSED' && <Btn small kind="primary" onClick={() => doAct('resume')}>재개</Btn>}
          {['RUNNING', 'WAITING_APPROVAL', 'WAITING_INPUT', 'WAITING_AI', 'BLOCKED', 'PAUSED'].includes(w.status) && <Btn small kind="danger" onClick={() => doAct('stop')}>중단</Btn>}
          {['FAILED', 'STOPPED', 'BLOCKED'].includes(w.status) && <Btn small kind="primary" onClick={() => doAct('retry')}>마지막 체크포인트부터 다시 실행</Btn>}
        </div>
      </div>
    </Modal>
  )
}

export function Metrics({ m }: { m: any }) {
  const cell = (label: string, v: any, unit = '') => <div className="bg-cream border border-brown/30 rounded p-1.5 text-center"><div className="font-bold">{v === null || v === undefined ? '-' : `${v}${unit}`}</div><div className="text-[10px] text-stone-500">{label}</div></div>
  return (
    <div>
      <div className="grid grid-cols-4 gap-1 text-xs">
        {cell('작업 성공률', m.success_rate, '%')}{cell('검증 통과율', m.verify_pass_rate, '%')}{cell('오류율', m.error_rate, '%')}{cell('평균 재시도', m.avg_retries)}
        {cell('사용자 수정률', m.correction_rate, '%')}{cell('평균 완료 시간', m.avg_ms === null ? null : m.avg_ms < 1000 ? `${m.avg_ms}ms` : `${(m.avg_ms / 1000).toFixed(1)}초`)}{cell('만족도', m.satisfaction, '점')}{cell('처리 건수', m.units)}
      </div>
      <p className="text-[11px] text-stone-500 mt-1">실제 처리 기록으로 계산한 값이며 게임 경험치·레벨과 별개입니다. 검증 통과율 = 첫 시도에 검증을 통과한 비율, 사용자 수정률 = 직원이 만든 기록 {m.produced}건 중 사용자가 고친 비율.</p>
    </div>
  )
}


/** 작업 분해 트리 (DAG): 선행 관계 깊이별로 열을 나눠 그린다 */
function Dag({ plan, name }: { plan: any[]; name: (id: string) => string }) {
  const depth: Record<string, number> = {}
  const d = (x: any): number => depth[x.id] ?? (depth[x.id] = x.depends_on.length ? 1 + Math.max(...x.depends_on.map((k: string) => d(plan.find((p) => p.id === k)))) : 0)
  plan.forEach(d)
  const cols: any[][] = []
  plan.forEach((x) => { (cols[depth[x.id]] ??= []).push(x) })
  const W = 128, H = 46, GX = 30, GY = 12
  const pos: Record<string, { x: number; y: number }> = {}
  cols.forEach((c, i) => c.forEach((x, j) => { pos[x.id] = { x: i * (W + GX), y: j * (H + GY) } }))
  const width = cols.length * (W + GX), height = Math.max(...cols.map((c) => c.length)) * (H + GY)
  const color: Record<string, string> = { DONE: '#bfe8d6', SKIPPED: '#e7e5e4', FAILED: '#fecaca', RUNNING: '#cfe8f7', FAILED_ATTEMPT: '#ffe9a8', WAITING_AI: '#cfe8f7', WAITING_INPUT: '#f6a192', PENDING: '#ffffff' }
  const label: Record<string, string> = { DONE: '완료', SKIPPED: '건너뜀', FAILED: '실패', RUNNING: '실행 중', FAILED_ATTEMPT: '검증·수정 중', WAITING_AI: 'AI 대기', WAITING_INPUT: '승인 대기', PENDING: '대기' }
  return (
    <div className="overflow-x-auto border border-brown/30 rounded bg-white p-1" aria-label="작업 분해 트리">
      <svg width={width} height={height} className="text-[10px]">
        {plan.flatMap((x) => x.depends_on.map((k: string) => { const a = pos[k], b = pos[x.id]; return <path key={k + x.id} d={`M${a.x + W} ${a.y + H / 2} C${a.x + W + GX / 2} ${a.y + H / 2} ${b.x - GX / 2} ${b.y + H / 2} ${b.x} ${b.y + H / 2}`} stroke="#7a5a3c" fill="none" strokeWidth="1.2" /> }))}
        {plan.map((x) => { const p = pos[x.id]; return (
          <g key={x.id} transform={`translate(${p.x},${p.y})`}>
            <rect width={W} height={H} rx="5" fill={color[x.status] ?? '#fff'} stroke="#7a5a3c" />
            <text x="6" y="14" fontWeight="bold" fill="#4a3a2a">{x.id} · {name(x.agent)}</text>
            <text x="6" y="27" fill="#4a3a2a">{x.goal.slice(0, 12)}</text>
            <text x="6" y="40" fill="#6b5a4a">{label[x.status] ?? x.status}{x.verify ? (x.verify.passed ? ' · 검증✔' : ' · 검증✘') : ''}</text>
          </g>) })}
      </svg>
    </div>
  )
}

function Revise({ w }: { w: any }) {
  const [req, setReq] = useState('')
  const refresh = useRefresh()
  const can = ['RUNNING', 'WAITING_APPROVAL', 'WAITING_INPUT', 'WAITING_AI', 'BLOCKED', 'PAUSED', 'FAILED', 'STOPPED'].includes(w.status)
  const send = async () => {
    if (!req.trim()) return
    const r = await act(() => api(`/api/workflows/${w.id}/revise`, { method: 'POST', body: { request: req } }))
    if (r) { useUI.getState().notify(r.status === 'applied' ? `계획 v${r.new_version}로 반영 — 다시 실행: ${r.affected_steps.join(', ') || '없음'}` : '자동 반영할 조건이 없어 기록만 했습니다'); setReq(''); refresh() }
  }
  const prio = async (n: number) => { await act(() => api(`/api/workflows/${w.id}/action`, { method: 'POST', body: { action: 'priority', rating: n } })); refresh() }
  return (
    <div className="space-y-1">
      {can && <>
        <div className="flex gap-2"><input className={input} value={req} maxLength={500} onChange={(e) => setReq(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="진행 중 조건 추가: 예) 슬라이드는 5장으로 · 워드 파일로 · 오후 4시로 바꿔줘 · 급해" /><Btn onClick={send}>조건 추가</Btn></div>
        <div className="flex items-center gap-1 text-xs">우선순위 {[0, 1, 2, 3].map((n) => <button key={n} onClick={() => prio(n)}><Badge cls={(w.priority ?? 0) === n ? 'bg-mint' : ''}>{['보통', '조금 높음', '높음', '가장 높음'][n]}</Badge></button>)}</div>
        <p className="text-[11px] text-stone-500">바뀐 조건에 영향받는 단계와 그 뒤 단계만 다시 실행하고, 이전 결과물은 그대로 보관합니다. 일정·문서를 바꾸는 단계가 다시 실행되면 승인을 다시 받습니다.</p>
      </>}
      {w.revisions?.length > 0 && <details><summary className="font-bold cursor-pointer text-sm">수정 이력 ({w.revisions.length})</summary>
        <ul className="text-xs space-y-1 mt-1">{w.revisions.map((r: any) => <li key={r.id}>{fmtTime(r.created_at)} v{r.base_version}→v{r.new_version} 「{r.revision_request}」 — {r.changes.join('; ') || r.note}{r.affected_steps.length ? ` · 다시 실행 ${r.affected_steps.join(', ')}` : ''}{Object.keys(r.previous_outputs || {}).length ? ` · 이전 산출물 보관 ${Object.keys(r.previous_outputs).join(', ')}` : ''}</li>)}</ul></details>}
    </div>
  )
}
