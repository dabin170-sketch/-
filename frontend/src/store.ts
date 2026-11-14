import { create } from 'zustand'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api'

export type AIState = { status: 'off' | 'unsupported' | 'loading' | 'ready' | 'error'; progress: number; text: string; model: string }

type UI = {
  page: string
  agentId: string | null
  taskId: string | null
  flowId: string | null
  live: Record<string, string>          // SSE로 받은 직원별 실시간 상태
  liveSync: string
  online: boolean
  lowPower: boolean
  ai: AIState
  toast: { msg: string; kind: 'ok' | 'err' } | null
  setPage: (p: string) => void
  openAgent: (id: string | null) => void
  openTask: (id: string | null) => void
  openFlow: (id: string | null) => void
  set: (p: Partial<UI>) => void
  setAI: (p: Partial<AIState>) => void
  notify: (msg: string, kind?: 'ok' | 'err') => void
}

const mobile = typeof navigator !== 'undefined' && /Android|iPhone|iPad/i.test(navigator.userAgent)

export const useUI = create<UI>((set) => ({
  page: location.hash.slice(1) || 'dashboard',
  agentId: null, taskId: null, flowId: null, live: {}, liveSync: '', online: true,
  lowPower: localStorage.getItem('lifeos.lowPower') === null ? mobile : localStorage.getItem('lifeos.lowPower') === '1',
  ai: { status: 'off', progress: 0, text: '', model: localStorage.getItem('lifeos.aiModel') || '' },
  toast: null,
  setPage: (page) => { location.hash = page; set({ page }) },
  openAgent: (agentId) => set({ agentId }),
  openTask: (taskId) => set({ taskId }),
  openFlow: (flowId) => set({ flowId, agentId: null }),
  set: (p) => set(p),
  setAI: (p) => set((s) => ({ ai: { ...s.ai, ...p } })),
  notify: (msg, kind = 'ok') => {
    set({ toast: { msg, kind } })
    setTimeout(() => set((s) => (s.toast?.msg === msg ? { toast: null } : {})), 3500)
  },
}))

/** 서버 상태 전체 (작은 개인 데이터이므로 한 번에 받는다). SSE가 변경을 알리면 다시 받는다. */
export function useLife() {
  return useQuery({ queryKey: ['state'], queryFn: () => api('/api/state'), refetchInterval: 15000, retry: false })
}
export function useSchema() {
  return useQuery({ queryKey: ['schema'], queryFn: () => api('/api/schema'), staleTime: Infinity })
}
export function useRefresh() {
  const qc = useQueryClient()
  return () => qc.invalidateQueries({ queryKey: ['state'] })
}

/** 오류를 알림으로 보여 주는 실행 도우미 */
export async function act(fn: () => Promise<any>, okMsg?: string) {
  try {
    const r = await fn()
    if (r?.queued) useUI.getState().notify('서버에 닿지 않아 기기에 보관했습니다. 연결되면 자동으로 보냅니다.')
    else if (okMsg) useUI.getState().notify(okMsg)
    return r
  } catch (e: any) {
    useUI.getState().notify(e?.message || '오류가 발생했습니다', 'err')
    return null
  }
}
