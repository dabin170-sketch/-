import { Office } from '../Office'
import { ApprovalBox } from '../Panels'
import { Command, WF_STATUS } from './Flows'
import { Discoveries } from './V3'
import { useLife, useUI } from '../store'
import { Badge, Bar, Btn, Card, Empty, Lines, STATUS, TASK_COLS, fmtTime } from '../ui'

export function Dashboard() {
  const { data: st } = useLife()
  const { live, openAgent, openTask, setPage, ai } = useUI()
  if (!st) return null
  const d = st.dashboard, ses = st.session, rep = ses?.report
  const running = st.tasks.filter((t: any) => ['RUNNING', 'ANALYZING'].includes(t.status))
  const pending = st.approvals.filter((a: any) => a.status === 'PENDING')
  const present = st.agents.filter((a: any) => (live[a.id] ?? a.status) !== 'OFFLINE').length
  const flows = st.workflows.filter((w: any) => ['RUNNING', 'WAITING_APPROVAL', 'WAITING_INPUT', 'WAITING_AI', 'BLOCKED'].includes(w.status))
  const props = st.workflows.filter((w: any) => w.status === 'PROPOSED')
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Card title="무엇을 맡길까요?" className="md:col-span-2"><Command />
        {(flows.length > 0 || props.length > 0) && <div className="flex flex-wrap gap-1.5 mt-2 text-sm">
          {flows.map((w: any) => <button key={w.id} onClick={() => { setPage('flows'); useUI.getState().openFlow(w.id) }}><Badge cls={WF_STATUS[w.status][1]}>{WF_STATUS[w.status][0]} · {w.title}</Badge></button>)}
          {props.length > 0 && <button onClick={() => setPage('flows')}><Badge cls="bg-sky">직원 제안 {props.length}건</Badge></button>}</div>}
      </Card>
      <Card title="💡 AI가 발견한 일" right={<Btn small onClick={() => setPage('discover')}>전체</Btn>}><Discoveries compact /></Card>
      <Card title="수집함 · 검수">
        <ul className="text-sm space-y-1">
          <li><button className="underline decoration-dotted" onClick={() => setPage('intake')}>수집함 승인 대기 후보 {st.intake_pending}건</button> · 확인 필요 {st.intake_summary?.needs_review ?? 0} · 분석 대기 {st.intake_summary?.queued ?? 0}</li>
          <li><button className="underline decoration-dotted" onClick={() => setPage('verify')}>검수 실패 {st.verify_summary?.failed ?? 0}건</button> / 전체 검수 {st.verify_summary?.total ?? 0}건</li>
          {(st.verify_summary?.recent_failed ?? []).slice(0, 3).map((v: any) => <li key={v.id} className="text-xs text-red-800">✘ {v.title || v.artifact_id}: {v.checks.find((c: any) => !c.passed)?.check_type}</li>)}
        </ul>
      </Card>
      <Card title={`오늘 ${d.today}`} right={<Badge cls={ses ? 'bg-mint' : 'bg-stone-200'}>{ses ? '접속 세션 진행 중' : '세션 없음'}</Badge>}>
        <p className="text-sm text-ink">직원 출근 {present}/12명 · {ai.status === 'ready' ? '로컬 AI 켜짐' : '규칙 기반 모드 (AI 꺼짐)'}</p>
        {ses?.limit && <p className="text-sm bg-sun border border-brown/40 rounded p-1.5 mt-1">⚠ {ses.limit} — 남은 업무는 대기 중입니다.</p>}
        {st.settings.paused && <p className="text-sm bg-stone-200 rounded p-1.5 mt-1">자동화가 일시중지되어 있습니다. (설정에서 재개)</p>}
        <div className="flex flex-wrap gap-1 mt-2">
          {st.agents.map((a: any) => { const s = live[a.id] ?? a.status; return <button key={a.id} onClick={() => openAgent(a.id)}><Badge cls={STATUS[s]?.cls}>{a.name} {STATUS[s]?.label}</Badge></button> })}
        </div>
      </Card>

      {rep && <Card title="재접속 보고서">
        <ul className="text-sm space-y-0.5 text-ink">
          <li>· {rep.first ? '첫 접속입니다. 직원 12명을 배치했습니다.' : `마지막 접속 ${fmtTime(rep.last_seen)}${rep.gap_days ? ` (${rep.gap_days}일 전)` : ''}`}</li>
          {rep.workflows?.length > 0 && <li>· 이어서 실행할 워크플로 {rep.workflows.length}건: {rep.workflows.join(', ')}</li>}
          {rep.proposals > 0 && <li>· 직원들이 제안한 후속 작업 {rep.proposals}건 (업무 실행 화면에서 수락·거절)</li>}
          {rep.recovered.length > 0 && <li>· 중단됐던 작업 {rep.recovered.length}건을 체크포인트부터 이어서 실행: {rep.recovered.join(', ')}</li>}
          {rep.skipped.length > 0 && <li>· 기간이 지나 실행하지 않고 생략한 업무 {rep.skipped.length}건: {rep.skipped.join(', ')}</li>}
          <li>· 기한이 지난 미완료 항목 {rep.overdue}건 · 3일 내 마감 {rep.deadlines}건 · 승인 대기 {rep.pending_approvals}건</li>
          {rep.inbox.map((i: any) => <li key={i.id}>· [정기 점검 {fmtTime(i.ts)}] {i.message}</li>)}
        </ul>
      </Card>}

      {pending.length > 0 && <Card title={`승인이 필요한 업무 ${pending.length}건`} className="md:col-span-2"><div className="space-y-2">{pending.map((a: any) => <ApprovalBox key={a.id} ap={a} />)}</div></Card>}

      <Card title="총괄 관리자 루미의 보고" right={<Badge>규칙 기반 집계</Badge>}>
        <Lines lines={d.report} />
        {d.alerts.length > 0 && <div className="mt-2 bg-coral/30 border border-brown/40 rounded p-2"><b className="text-sm">중요한 알림</b><Lines lines={d.alerts} /></div>}
      </Card>

      <Card title="지금 진행 중인 업무" right={<Btn small onClick={() => setPage('tasks')}>업무 센터</Btn>}>
        {running.length === 0 ? <Empty>{ses ? '진행 중인 업무가 없습니다. 할 일이 생기면 직원이 스스로 시작합니다.' : '접속 세션이 시작되면 표시됩니다.'}</Empty> :
          running.map((t: any) => <button key={t.id} className="block w-full text-left mb-2" onClick={() => openTask(t.id)}>
            <div className="flex justify-between text-sm"><span className="truncate">{t.title}</span><span className="text-xs text-stone-500 shrink-0">{st.agents.find((a: any) => a.id === t.assigned_agent)?.name} · {t.checkpoint.step}/{t.steps.length}</span></div><Bar pct={t.progress} /></button>)}
      </Card>

      <Card title="오늘의 일정" right={<Btn small onClick={() => setPage('planner')}>일정·목표</Btn>}>
        {d.events.length === 0 ? <Empty>오늘 등록된 일정이 없습니다.</Empty> :
          <ul className="text-sm space-y-1">{d.events.map((e: any) => <li key={e.id} className={e.done ? 'line-through text-stone-400' : ''}><span className="font-mono text-xs mr-2">{e.start ? `${e.start}${e.end ? '~' + e.end : ''}` : '종일'}</span>{e.title} {e.type !== '일정' && <Badge>{e.type}</Badge>}</li>)}</ul>}
      </Card>

      <Card title="통계">
        <div className="grid grid-cols-3 gap-2 text-center">
          {[['오늘 완료', d.done_today], ['최근 7일 완료', d.done_week], ['실패 누적', d.failed]].map(([l, v]) => <div key={l as string} className="bg-cream border border-brown/40 rounded p-2"><div className="text-xl font-bold text-ink">{v as number}</div><div className="text-xs text-stone-500">{l}</div></div>)}
        </div>
        <div className="mt-2 text-sm">목표 달성률 {d.goals.rate === null ? <span className="text-stone-500">— 목표 수치가 입력된 목표가 없습니다</span> : <b>{d.goals.rate}% (진행 중 {d.goals.count}개)</b>}
          {d.goals.rate !== null && <Bar pct={d.goals.rate} />}</div>
      </Card>
    </div>
  )
}

export function OfficePage() {
  const { data: st } = useLife()
  const { lowPower, set, live } = useUI()
  if (!st) return null
  const toggle = () => { localStorage.setItem('lifeos.lowPower', lowPower ? '0' : '1'); set({ lowPower: !lowPower }) }
  const recent = st.logs.slice(-8).reverse()
  return (
    <div className="space-y-3">
      <Office agents={st.agents} tasks={st.tasks} />
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        {Object.entries(STATUS).map(([k, v]) => <Badge key={k} cls={v.cls}>{v.label} {st.agents.filter((a: any) => (live[a.id] ?? a.status) === k).length}</Badge>)}
        <span className="grow" /><Btn small onClick={toggle}>{lowPower ? '저사양 모드 켜짐 (4fps)' : '저사양 모드 꺼짐'}</Btn>
      </div>
      <p className="text-xs text-stone-500">캐릭터 동작은 서버의 실제 작업 상태와 연결되어 있습니다. 실행 중인 업무가 없으면 휴게실에서 쉽니다. 캐릭터를 누르면 직원 패널이 열립니다. 위쪽 화이트보드 앞은 계획 중, 오른쪽 위는 회의실, 오른쪽 아래는 휴게실입니다.</p>
      <Card title="사무실 소식"><ul className="text-sm space-y-0.5">{recent.map((l: any) => <li key={l.id}><span className="text-xs text-stone-500">{fmtTime(l.ts)}</span> <b>{st.agents.find((a: any) => a.id === l.agent_id)?.name}</b> {l.message}</li>)}</ul>
        {recent.length === 0 && <Empty>아직 소식이 없습니다.</Empty>}</Card>
    </div>
  )
}

export function Tasks() {
  const { data: st } = useLife()
  const { openTask } = useUI()
  if (!st) return null
  const name = (id: string) => st.agents.find((a: any) => a.id === id)?.name
  const pending = st.approvals.filter((a: any) => a.status === 'PENDING')
  return (
    <div className="space-y-3">
      {pending.length > 0 && <Card title={`승인 요청 ${pending.length}건`}><div className="space-y-2">{pending.map((a: any) => <ApprovalBox key={a.id} ap={a} />)}</div></Card>}
      <div className="flex gap-2 overflow-x-auto pb-2 snap-x">
        {TASK_COLS.map(([key, label]) => {
          const items = st.tasks.filter((t: any) => t.status === key || (key === 'QUEUED' && t.status === 'BLOCKED'))
            .sort((a: any, b: any) => ['DONE', 'FAILED', 'STOPPED'].includes(key) ? b.updated_at.localeCompare(a.updated_at) : b.priority - a.priority)
          return (
            <div key={key} className="snap-start shrink-0 w-56 bg-paper border-2 border-brown rounded-lg shadow-pxs">
              <div className="px-2 py-1 border-b-2 border-brown bg-sun rounded-t-md text-sm font-bold flex justify-between"><span>{label}</span><span>{items.length}</span></div>
              <div className="p-1.5 space-y-1.5 max-h-[60vh] overflow-y-auto min-h-[4rem]">
                {items.slice(0, 40).map((t: any) => (
                  <button key={t.id} onClick={() => openTask(t.id)} className="block w-full text-left bg-white border border-brown/50 rounded p-1.5 text-xs hover:bg-cream">
                    <div className="font-semibold text-ink break-words">{t.title}</div>
                    <div className="flex justify-between text-stone-500 mt-0.5"><span>{name(t.assigned_agent)}</span><span>{t.status === 'BLOCKED' ? '⏸ 막힘' : `${t.priority}점`}</span></div>
                    {t.status === 'RUNNING' && <Bar pct={t.progress} />}
                    {t.waiting_ai && <div className="text-sky-700">로컬 AI 응답 대기</div>}
                  </button>
                ))}
              </div>
            </div>
          )
        })}
      </div>
      <p className="text-xs text-stone-500">카드를 누르면 근거·우선순위 산정·단계별 로그를 볼 수 있습니다. 진행률은 실제로 끝난 단계 수로만 계산합니다.</p>
    </div>
  )
}

export function Agents() {
  const { data: st } = useLife()
  const { openAgent, live } = useUI()
  if (!st) return null
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {st.agents.map((a: any) => {
        const s = live[a.id] ?? a.status
        const cur = st.tasks.find((t: any) => t.id === a.current_task)
        return (
          <button key={a.id} onClick={() => openAgent(a.id)} className="text-left bg-paper border-2 border-brown rounded-lg shadow-px p-3 hover:bg-cream">
            <div className="flex gap-2 items-center">
              <div className="w-10 h-10 rounded border-2 border-brown flex items-center justify-center font-bold" style={{ background: a.color }}>{a.name[0]}</div>
              <div className="min-w-0"><div className="font-bold text-ink">{a.name} <span className="font-normal text-xs text-stone-500">{a.title}</span></div>
                <div className="flex gap-1 flex-wrap"><Badge cls={STATUS[s]?.cls}>{STATUS[s]?.label}</Badge><Badge cls="bg-sun">Lv.{a.level} {a.level_label}</Badge><Badge>신뢰도 {a.trust}</Badge></div></div>
            </div>
            <p className="text-xs text-stone-600 mt-2 truncate">{cur ? `▶ ${cur.title}` : a.specialty}</p>
            <p className="text-xs text-stone-500">경험치 {a.experience} · 실제 성공률 {a.metrics.success_rate ?? '-'}% · 검증 통과율 {a.metrics.verify_pass_rate ?? '-'}% · 수정률 {a.metrics.correction_rate ?? '-'}%</p>
          </button>
        )
      })}
    </div>
  )
}
