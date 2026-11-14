import { useEffect, useRef } from 'react'
import { useUI } from './store'
import { STATUS } from './ui'

// 논리 해상도 320x200 픽셀 캔버스를 화면 크기에 맞게 확대한다 (image-rendering: pixelated).
const W = 320, H = 200
const desk = (i: number) => ({ x: 14 + (i % 4) * 48, y: 70 + Math.floor(i / 4) * 40 })
const SEAT = (i: number) => ({ x: desk(i).x, y: desk(i).y - 11 })
const LOUNGE = (i: number) => ({ x: 217 + (i % 4) * 24, y: 101 + Math.floor(i / 4) * 32 })
const BOARD = (i: number) => ({ x: 96 + (i % 6) * 13, y: 40 + Math.floor(i / 6) * 6 })
const DOOR = { x: 196, y: 190 }
const HAIR = ['#5b3a29', '#2d2a32', '#8a5a2b', '#c98b4a', '#3b4a6b', '#7a3e48']

type Sprite = { x: number; y: number; status: string; moving: boolean }

function target(status: string, i: number) {
  if (status === 'OFFLINE') return DOOR
  if (status === 'IDLE' || status === 'PAUSED') return LOUNGE(i)
  if (status === 'PLANNING') return BOARD(i)
  return SEAT(i) // CHECKING_IN, WORKING, WAITING_APPROVAL, BLOCKED, ERROR, COMPLETED
}

function drawRoom(c: CanvasRenderingContext2D, lit: boolean, f: number, working: Set<number>) {
  const R = (x: number, y: number, w: number, h: number, col: string) => { c.fillStyle = col; c.fillRect(x, y, w, h) }
  R(0, 0, W, 52, '#f0e2c4'); R(0, 50, W, 2, '#c9a97a')                       // 벽
  R(0, 52, W, H - 52, '#fbf1da')                                              // 바닥
  for (let x = 0; x < W; x += 16) R(x, 52, 1, H - 52, '#f1e3c4')
  for (let y = 52; y < H; y += 16) R(0, y, W, 1, '#f1e3c4')
  for (const wx of [14, 250, 286]) {                                          // 창문
    R(wx, 10, 26, 26, '#7a5a3c'); R(wx + 2, 12, 22, 22, lit ? '#cfe8f7' : '#3d4a6b')
    R(wx + 12, 12, 2, 22, '#7a5a3c'); R(wx + 2, 22, 22, 2, '#7a5a3c')
    if (lit) R(wx + 4, 14, 5, 3, '#ffffff')
  }
  R(52, 12, 32, 24, '#7a5a3c'); R(54, 14, 28, 20, '#e9c98f')                  // 게시판
  R(57, 17, 7, 6, '#ffe9a8'); R(67, 16, 8, 7, '#bfe8d6'); R(60, 26, 9, 5, '#f6a192'); R(72, 26, 6, 5, '#cfe8f7')
  R(96, 8, 82, 30, '#7a5a3c'); R(98, 10, 78, 26, '#ffffff')                   // 화이트보드
  R(102, 14, 30, 1, '#8fd3b6'); R(102, 18, 44, 1, '#f6a192'); R(102, 22, 22, 1, '#8fb8d3'); R(150, 14, 20, 14, '#fff6d6')
  R(184, 26, 6, 26, '#9b7b55'); R(190, 14, 20, 38, '#b08a5e'); R(205, 32, 2, 3, '#ffe9a8') // 문(벽 쪽 장식)
  R(210, 60, 2, 130, '#e6d3ad')                                               // 구역 구분
  R(224, 66, 74, 26, '#c79a63'); R(224, 90, 74, 3, '#9b7647')                 // 회의 테이블
  for (let k = 0; k < 4; k++) { R(230 + k * 17, 59, 9, 6, '#f6a192'); R(230 + k * 17, 95, 9, 6, '#8fd3b6') }
  R(218, 118, 60, 6, '#8fb8d3'); R(218, 124, 60, 4, '#6f9ab8')                // 휴게실 소파
  R(288, 108, 14, 18, '#7a5a3c'); R(290, 110, 10, 7, '#4a3a2a'); R(292, 119, 6, 4, '#ffffff') // 커피머신
  if (lit && f % 8 < 4) R(294, 106, 1, 2, '#d8cbb4')
  for (const [px, py] of [[304, 178], [4, 178], [200, 62]] as const) {        // 화분
    R(px + 2, py + 8, 8, 8, '#c9764f'); R(px + 3, py, 6, 9, '#6fbf8f'); R(px, py + 3, 4, 5, '#8fd3a6'); R(px + 8, py + 2, 4, 6, '#8fd3a6')
  }
  R(4, 58, 10, 3, '#b08a5e'); R(4, 110, 3, 40, '#b08a5e')                     // 책장
  for (let k = 0; k < 6; k++) R(5, 112 + k * 6, 2, 5, ['#f6a192', '#8fd3b6', '#ffe9a8', '#8fb8d3'][k % 4])
  for (let i = 0; i < 12; i++) {                                              // 책상 12개
    const d = desk(i)
    R(d.x, d.y - 4, 10, 4, '#a8c4d8')                                      // 의자 등받이
    R(d.x, d.y, 34, 10, '#c79a63'); R(d.x, d.y + 10, 34, 2, '#9b7647')
    R(d.x + 2, d.y + 12, 2, 6, '#9b7647'); R(d.x + 30, d.y + 12, 2, 6, '#9b7647')
    R(d.x + 11, d.y + 1, 12, 7, '#4a3a2a')                                     // 모니터
    if (working.has(i)) {
      R(d.x + 12, d.y + 2, 10, 5, '#dff5ea')
      for (let k = 0; k < 3; k++) R(d.x + 13, d.y + 2 + k * 2 - 0, 3 + ((f + k * 3 + i) % 6), 1, '#5aa688')
    } else R(d.x + 12, d.y + 2, 10, 5, '#2f2a26')
    R(d.x + 26, d.y + 3, 4, 4, '#ffffff'); R(d.x + 27, d.y + 4, 2, 2, '#b98a5b') // 머그컵
  }
}

function drawAgent(c: CanvasRenderingContext2D, a: any, i: number, sp: Sprite, f: number) {
  const R = (x: number, y: number, w: number, h: number, col: string) => { c.fillStyle = col; c.fillRect(Math.round(x), Math.round(y), w, h) }
  const st = sp.moving ? 'WALK' : sp.status
  let { x, y } = sp
  if (st === 'COMPLETED') y -= Math.abs(Math.sin(f / 2)) * 4
  const seated = !sp.moving && ['WORKING', 'WAITING_APPROVAL', 'BLOCKED', 'ERROR', 'CHECKING_IN', 'COMPLETED'].includes(sp.status)
  R(x + 1, y - 1, 8, 3, HAIR[i % HAIR.length])                                // 머리카락
  if (i % 3 === 1) { R(x, y + 1, 1, 5, HAIR[i % HAIR.length]); R(x + 9, y + 1, 1, 5, HAIR[i % HAIR.length]) }
  if (i % 3 === 2) R(x + 3, y - 3, 4, 2, HAIR[i % HAIR.length])
  R(x + 1, y + 2, 8, 6, '#ffe0bd')                                             // 얼굴
  if (st === 'ERROR') { R(x + 2, y + 4, 2, 1, '#b3261e'); R(x + 6, y + 4, 2, 1, '#b3261e'); R(x + 4, y + 6, 2, 1, '#4a3a2a') }
  else if (st === 'PAUSED' || (st === 'IDLE' && f % 40 > 36)) { R(x + 2, y + 5, 2, 1, '#4a3a2a'); R(x + 6, y + 5, 2, 1, '#4a3a2a') }
  else { R(x + 3, y + 4, 1, 2, '#4a3a2a'); R(x + 6, y + 4, 1, 2, '#4a3a2a') }
  R(x + 2, y + 6, 1, 1, '#f6b7a8'); R(x + 7, y + 6, 1, 1, '#f6b7a8')           // 볼터치
  R(x + 1, y + 8, 8, 5, a.color)                                               // 몸
  if (a.level >= 2) R(x + 4, y + 9, 2, 2, '#fff3a8')                            // 레벨 장식: 배지
  if (a.level >= 4) { R(x + 2, y - 3, 6, 1, '#f2c230'); R(x + 2, y - 4, 1, 1, '#f2c230'); R(x + 4, y - 4, 2, 1, '#f2c230'); R(x + 7, y - 4, 1, 1, '#f2c230') } // 왕관
  if (!seated) {                                                               // 다리
    const s = sp.moving ? (f % 4 < 2 ? 1 : 0) : 0
    R(x + 2, y + 13, 2, 2 + s, '#4a3a2a'); R(x + 6, y + 13, 2, 3 - s, '#4a3a2a')
  }
  // 상태별 동작
  if (st === 'WORKING') { const up = f % 2; R(x - 1, y + 10 - up, 2, 2, '#ffe0bd'); R(x + 9, y + 9 + up, 2, 2, '#ffe0bd') }
  else if (st === 'WAITING_APPROVAL') { R(x + 9, y + 2 - (f % 4 < 2 ? 1 : 0), 2, 7, '#ffe0bd'); R(x + 11, y - 7, 7, 8, '#f6a192'); R(x + 14, y - 6, 1, 4, '#fff'); R(x + 14, y - 1, 1, 1, '#fff') }
  else if (st === 'PLANNING') { R(x + 9, y + 9, 4, 4, '#fff'); R(x + 10, y + 10, 2, 1, '#8fb8d3'); for (let k = 0; k < 3; k++) if ((f >> 1) % 4 > k) R(x + 2 + k * 3, y - 5, 2, 2, '#7a5a3c') }
  else if (st === 'ERROR') { if (f % 4 < 2) { R(x + 11, y - 7, 5, 8, '#e5484d'); R(x + 13, y - 6, 1, 4, '#fff'); R(x + 13, y - 1, 1, 1, '#fff') } R(x - 2, y + 2 + (f % 3), 1, 2, '#6fb6e8') }
  else if (st === 'BLOCKED') { R(x + 11, y - 6, 6, 5, '#9aa0a6'); R(x + 12, y - 8, 4, 3, '#9aa0a6'); R(x + 13, y - 7, 2, 2, '#fbf1da'); R(x + 13, y - 4, 2, 2, '#4a3a2a') }
  else if (st === 'COMPLETED') { for (let k = 0; k < 6; k++) R(x - 3 + ((k * 5 + f * 2) % 16), y - 6 + ((k * 7 + f) % 10), 1, 1, ['#f6a192', '#8fd3b6', '#ffe08a', '#8fb8d3'][k % 4]); R(x - 1, y + 5, 2, 3, '#ffe0bd'); R(x + 9, y + 5, 2, 3, '#ffe0bd') }
  else if (st === 'IDLE') {
    if (i % 2 === 0) { R(x + 9, y + 9, 3, 3, '#fff'); R(x + 12, y + 10, 1, 1, '#fff'); if (f % 6 < 3) R(x + 10, y + 6, 1, 2, '#d8cbb4') } // 커피
    else { R(x + 8, y + 9, 5, 4, '#f6a192'); R(x + 10, y + 9, 1, 4, '#fff') }                                                         // 책
  }
  else if (st === 'PAUSED') { c.fillStyle = '#7a5a3c'; c.font = '6px monospace'; c.fillText('z', x + 10, y - 1 - (f % 6) / 2); c.fillText('z', x + 13, y - 5 - (f % 6) / 2) }
  else if (st === 'WALK') R(x + 9, y + 9, 3, 4, '#9b7647')                      // 출근 가방
}

export function Office({ agents, tasks }: { agents: any[]; tasks: any[] }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const labels = useRef<(HTMLButtonElement | null)[]>([])
  const data = useRef({ agents, live: {} as Record<string, string> })
  const sprites = useRef<Sprite[]>([])
  const { live, lowPower, openAgent } = useUI()
  data.current = { agents, live }

  useEffect(() => {
    const c = canvas.current!.getContext('2d')!
    c.imageSmoothingEnabled = false
    let raf = 0, last = 0, f = 0
    const interval = lowPower ? 250 : 90 // 저사양 모드: 4fps, 기본: 약 11fps
    const loop = (t: number) => {
      raf = requestAnimationFrame(loop)
      if (document.hidden || t - last < interval) return // 탭이 가려지면 그리지 않는다
      last = t; f++
      const { agents, live } = data.current
      const status = (a: any) => live[a.id] ?? a.status
      const lit = agents.some((a) => status(a) !== 'OFFLINE')
      const working = new Set<number>()
      agents.forEach((a, i) => {
        const st = status(a)
        const tg = target(st, i)
        let sp = sprites.current[i]
        if (!sp) { const atDoor = st === 'OFFLINE' || st === 'CHECKING_IN'; sp = sprites.current[i] = { x: atDoor ? DOOR.x : tg.x, y: atDoor ? DOOR.y : tg.y, status: st, moving: false } }
        const dx = tg.x - sp.x, dy = tg.y - sp.y, dist = Math.hypot(dx, dy)
        const speed = lowPower ? 14 : 5
        if (dist > speed) { sp.x += (dx / dist) * speed; sp.y += (dy / dist) * speed; sp.moving = true }
        else { sp.x = tg.x; sp.y = tg.y; sp.moving = false }
        sp.status = st
        if (st === 'WORKING' && !sp.moving) working.add(i)
      })
      drawRoom(c, lit, f, working)
      const order = agents.map((_, i) => i).sort((p, q) => sprites.current[p].y - sprites.current[q].y)
      for (const i of order) {
        const sp = sprites.current[i]
        const hidden = sp.status === 'OFFLINE' && !sp.moving
        if (!hidden) drawAgent(c, agents[i], i, sp, f)
        const el = labels.current[i]
        if (el) {
          el.style.left = `${((sp.x + 5) / W) * 100}%`
          el.style.top = `${((sp.y + 8) / H) * 100}%`
          el.style.display = hidden ? 'none' : 'block'
        }
      }
      if (!lit) { c.fillStyle = 'rgba(35,38,70,0.5)'; c.fillRect(0, 0, W, H) }   // 접속 전: 조명 꺼짐
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [lowPower])

  return (
    <div className="relative w-full border-2 border-brown rounded-lg shadow-px overflow-hidden bg-cream select-none" style={{ aspectRatio: `${W}/${H}` }}>
      <canvas ref={canvas} width={W} height={H} className="absolute inset-0 w-full h-full" style={{ imageRendering: 'pixelated' }} aria-label="픽셀 아트 사무실" />
      {agents.map((a, i) => {
        const st = live[a.id] ?? a.status
        const t = tasks.find((x) => x.id === a.current_task)
        const bubble = st === 'WAITING_APPROVAL' ? '승인해 주세요!' : st === 'WORKING' && t ? t.title : st === 'ERROR' ? '오류 발생' : st === 'COMPLETED' ? '완료!' : st === 'BLOCKED' ? '막혔어요' : ''
        return (
          <button key={a.id} ref={(el) => { labels.current[i] = el }} onClick={() => openAgent(a.id)}
            aria-label={`${a.name} ${a.title} — ${STATUS[st]?.label}`}
            className="absolute -translate-x-1/2 -translate-y-1/2 w-[9%] h-[14%] min-w-[28px] min-h-[36px] focus:outline-none focus-visible:ring-2 ring-coral rounded">
            {bubble && <span className="absolute left-1/2 -translate-x-1/2 bottom-full mb-[10%] bg-white border border-brown rounded px-1 text-[9px] sm:text-[11px] leading-tight text-ink whitespace-nowrap max-w-[9rem] truncate shadow-pxs pointer-events-none">{bubble}</span>}
            <span className="absolute left-1/2 -translate-x-1/2 top-full text-[8px] sm:text-[10px] font-bold text-ink bg-paper/80 px-0.5 rounded pointer-events-none whitespace-nowrap">{a.name}</span>
          </button>
        )
      })}
    </div>
  )
}
