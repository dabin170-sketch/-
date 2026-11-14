import { useState } from 'react'
import { mutate, newId } from './api'
import { act, useLife, useRefresh, useSchema } from './store'
import { Btn, Empty, Modal, input } from './ui'

type Props = {
  name: string
  columns?: string[]
  filter?: (r: any) => boolean
  sort?: (a: any, b: any) => number
  defaults?: Record<string, any>
  extra?: (r: any) => React.ReactNode
  limit?: number
}

/** 스키마 정의로 목록·추가·수정·삭제 화면을 그리는 공용 컴포넌트 */
export function Crud({ name, columns, filter, sort, defaults, extra, limit = 200 }: Props) {
  const { data: st } = useLife()
  const { data: schema } = useSchema()
  const refresh = useRefresh()
  const [edit, setEdit] = useState<any | null>(null)
  const spec = schema?.collections?.[name]
  if (!spec || !st) return null
  const all: any[] = Object.values(st.c?.[name] ?? {})
  let rows = filter ? all.filter(filter) : all
  rows = rows.sort(sort ?? ((a, b) => String(b.date ?? b.created_at ?? '').localeCompare(String(a.date ?? a.created_at ?? ''))))
  const fields: any[] = spec.fields
  const cols = (columns ?? fields.filter((f) => f.type !== 'textarea').slice(0, 4).map((f) => f.k)).map((k) => fields.find((f) => f.k === k)).filter(Boolean)

  const show = (f: any, r: any) => {
    const v = r[f.k]
    if (f.type === 'bool') return v ? '✔' : ''
    if (f.type === 'ref') return st.c?.[f.ref]?.[v]?.title ?? '(삭제됨)'
    if (f.option_labels) return f.option_labels[v] ?? v
    if (f.type === 'number' && typeof v === 'number') return v.toLocaleString()
    return v ?? ''
  }

  const save = async (form: any) => {
    const isNew = !edit.id
    const r = await act(() => isNew
      ? mutate('POST', `/api/c/${name}`, { ...form, id: newId(name.slice(0, 3)) })
      : mutate('PUT', `/api/c/${name}/${edit.id}`, form), '저장했습니다')
    if (r) { setEdit(null); refresh() }
  }
  const remove = async () => {
    if (!confirm('삭제할까요? 되돌릴 수 없습니다.')) return
    if (await act(() => mutate('DELETE', `/api/c/${name}/${edit.id}`), '삭제했습니다')) { setEdit(null); refresh() }
  }

  return (
    <div>
      <div className="flex justify-between items-center mb-2">
        <span className="text-xs text-stone-500">{spec.label} {rows.length}건</span>
        <Btn small kind="primary" onClick={() => setEdit({ ...(defaults ?? {}) })}>+ {spec.label} 추가</Btn>
      </div>
      {rows.length === 0 ? <Empty>아직 등록된 {spec.label}이(가) 없습니다.</Empty> : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-stone-500 border-b border-brown/30">
                {cols.map((f: any) => <th key={f.k} className="py-1 pr-2 font-semibold whitespace-nowrap">{f.label.replace(/\(.*\)/, '')}</th>)}
                {extra && <th />}
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, limit).map((r) => (
                <tr key={r.id} className="border-b border-brown/10 hover:bg-cream cursor-pointer" onClick={() => setEdit(r)}>
                  {cols.map((f: any) => <td key={f.k} className="py-1.5 pr-2 max-w-[14rem] truncate">{show(f, r)}</td>)}
                  {extra && <td className="py-1 text-right" onClick={(e) => e.stopPropagation()}>{extra(r)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {edit && <EditForm title={`${spec.label} ${edit.id ? '수정' : '추가'}`} fields={fields} initial={edit} st={st}
        onClose={() => setEdit(null)} onSave={save} onDelete={edit.id ? remove : undefined} />}
    </div>
  )
}

function EditForm({ title, fields, initial, st, onClose, onSave, onDelete }: any) {
  const [form, setForm] = useState<any>(() => {
    const f: any = {}
    for (const fd of fields) if (!fd.readonly) f[fd.k] = initial[fd.k] ?? (initial.id ? '' : fd.default ?? (fd.type === 'date' && fd.required ? st.today : ''))
    return f
  })
  const set = (k: string, v: any) => setForm((p: any) => ({ ...p, [k]: v }))
  return (
    <Modal title={title} onClose={onClose}>
      <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); onSave(form) }}>
        {fields.filter((f: any) => !f.readonly).map((f: any) => (
          <label key={f.k} className="block text-xs font-semibold text-ink">
            {f.label}{f.required && <span className="text-red-600"> *</span>}
            {f.type === 'textarea' ? <textarea className={input + ' h-32 font-mono'} value={form[f.k] ?? ''} onChange={(e) => set(f.k, e.target.value)} />
              : f.type === 'bool' ? <input type="checkbox" className="ml-2 w-5 h-5 align-middle" checked={!!form[f.k]} onChange={(e) => set(f.k, e.target.checked)} />
              : f.type === 'select' ? (
                <select className={input} value={form[f.k] ?? ''} onChange={(e) => set(f.k, e.target.value)}>
                  {!f.required && <option value="">(선택 안 함)</option>}
                  {f.required && !form[f.k] && <option value="">선택하세요</option>}
                  {f.options.map((o: string) => <option key={o} value={o}>{f.option_labels?.[o] ?? (st.agents.find((a: any) => a.id === o)?.name ? `${st.agents.find((a: any) => a.id === o).name} (${o})` : o)}</option>)}
                </select>
              ) : f.type === 'ref' ? (
                <select className={input} value={form[f.k] ?? ''} onChange={(e) => set(f.k, e.target.value)}>
                  <option value="">{f.required ? '선택하세요' : '(선택 안 함)'}</option>
                  {Object.values<any>(st.c?.[f.ref] ?? {}).map((r) => <option key={r.id} value={r.id}>{r.title}</option>)}
                </select>
              ) : <input className={input} type={f.type === 'number' ? 'number' : f.type === 'date' ? 'date' : f.type === 'time' ? 'time' : 'text'}
                step="any" min={f.min} max={f.max} value={form[f.k] ?? ''} required={f.required}
                onChange={(e) => set(f.k, e.target.value)} />}
          </label>
        ))}
        <div className="flex justify-between pt-2">
          {onDelete ? <Btn kind="danger" onClick={onDelete}>삭제</Btn> : <span />}
          <div className="flex gap-2"><Btn onClick={onClose}>취소</Btn><Btn kind="primary" type="submit">저장</Btn></div>
        </div>
      </form>
    </Modal>
  )
}
