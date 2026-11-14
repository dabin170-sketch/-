import { ReactNode } from 'react'

export const STATUS: Record<string, { label: string; cls: string }> = {
  OFFLINE: { label: '퇴근', cls: 'bg-stone-200 text-stone-600' },
  CHECKING_IN: { label: '출근 중', cls: 'bg-sky text-ink' },
  IDLE: { label: '대기', cls: 'bg-cream text-ink' },
  PLANNING: { label: '계획 중', cls: 'bg-sun text-ink' },
  WORKING: { label: '업무 중', cls: 'bg-mint text-ink' },
  WAITING_APPROVAL: { label: '승인 대기', cls: 'bg-coral text-ink' },
  BLOCKED: { label: '막힘', cls: 'bg-stone-300 text-ink' },
  ERROR: { label: '오류', cls: 'bg-red-200 text-red-900' },
  COMPLETED: { label: '완료!', cls: 'bg-emerald-200 text-emerald-900' },
  PAUSED: { label: '일시정지', cls: 'bg-stone-200 text-stone-600' },
}

export const TASK_COLS: [string, string][] = [
  ['NEW', '신규 작업'], ['ANALYZING', '분석 중'], ['QUEUED', '실행 대기'], ['RUNNING', '실행 중'],
  ['WAITING_APPROVAL', '승인 대기'], ['DONE', '완료'], ['FAILED', '실패'], ['STOPPED', '중단'],
]
export const taskLabel = (st: string) => (st === 'BLOCKED' ? '막힘' : TASK_COLS.find((c) => c[0] === st)?.[1] ?? st)

export function Card({ title, children, right, className = '' }: { title?: ReactNode; children: ReactNode; right?: ReactNode; className?: string }) {
  return (
    <section className={`bg-paper border-2 border-brown rounded-lg shadow-px p-3 ${className}`}>
      {(title || right) && (
        <div className="flex items-center justify-between gap-2 mb-2">
          <h2 className="font-bold text-ink text-[15px]">{title}</h2>
          {right}
        </div>
      )}
      {children}
    </section>
  )
}

export function Btn({ children, onClick, kind = 'default', disabled, small, type = 'button', title }: {
  children: ReactNode; onClick?: () => void; kind?: 'default' | 'primary' | 'danger' | 'ghost'; disabled?: boolean; small?: boolean; type?: 'button' | 'submit'; title?: string
}) {
  const k = { default: 'bg-paper', primary: 'bg-mint', danger: 'bg-coral', ghost: 'bg-transparent border-transparent shadow-none' }[kind]
  return (
    <button type={type} title={title} disabled={disabled} onClick={onClick}
      className={`${k} border-2 border-brown rounded-md ${kind === 'ghost' ? '' : 'shadow-pxs active:translate-x-[1px] active:translate-y-[1px] active:shadow-none'} ${small ? 'px-2 py-0.5 text-xs' : 'px-3 py-1.5 text-sm'} font-semibold text-ink whitespace-nowrap shrink-0 disabled:opacity-40 disabled:cursor-not-allowed`}>
      {children}
    </button>
  )
}

export function Badge({ children, cls = 'bg-cream text-ink' }: { children: ReactNode; cls?: string }) {
  return <span className={`inline-block px-1.5 py-0.5 rounded text-[11px] font-semibold border border-brown/40 whitespace-nowrap ${cls}`}>{children}</span>
}

export function Bar({ pct, color = '#8fd3b6' }: { pct: number; color?: string }) {
  return (
    <div className="h-2.5 w-full bg-cream border border-brown rounded-sm overflow-hidden">
      <div className="h-full" style={{ width: `${Math.max(0, Math.min(100, pct))}%`, background: color }} />
    </div>
  )
}

export function Modal({ title, onClose, children, wide }: { title: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean }) {
  return (
    <div className="fixed inset-0 z-40 bg-ink/40 flex items-end sm:items-center justify-center p-0 sm:p-4" onClick={onClose}>
      <div role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}
        className={`bg-paper border-2 border-brown rounded-t-xl sm:rounded-xl shadow-px w-full ${wide ? 'sm:max-w-3xl' : 'sm:max-w-lg'} max-h-[92vh] flex flex-col`}>
        <div className="flex items-center justify-between px-4 py-2 border-b-2 border-brown bg-sun rounded-t-[10px]">
          <h3 className="font-bold text-ink">{title}</h3>
          <button onClick={onClose} aria-label="닫기" className="text-ink font-bold px-2 text-lg">×</button>
        </div>
        <div className="p-4 overflow-y-auto">{children}</div>
      </div>
    </div>
  )
}

export const Empty = ({ children }: { children: ReactNode }) => <p className="text-sm text-stone-500 py-2">{children}</p>

export function Lines({ lines }: { lines?: string[] }) {
  if (!lines?.length) return null
  return <ul className="text-sm text-ink space-y-0.5">{lines.map((l, i) => <li key={i} className="whitespace-pre-wrap break-words">· {l}</li>)}</ul>
}

export const fmtTime = (iso?: string | null) => {
  if (!iso) return '-'
  const d = new Date(iso)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
export const input = 'w-full border-2 border-brown rounded-md px-2 py-1.5 bg-white text-sm text-ink'
