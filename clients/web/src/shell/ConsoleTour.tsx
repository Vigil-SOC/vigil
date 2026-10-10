import { useLayoutEffect, useRef, useState } from 'react'
import { NAV, type ConsoleScreenKey } from '../data/data'

export type TourStopId = 'nav' | 'attention' | 'ask'

// Tab names come from NAV so the copy follows a rename.
const tab = (key: ConsoleScreenKey) => NAV.find(n => n[2] === key)?.[1] ?? key

const COPY: Record<TourStopId, { title: string; body: string; selector: string }> = {
  nav: {
    title: 'Pages are grouped by what you are doing',
    body: `Watch intake (${tab('overview')}, ${tab('triage')}), work cases (${tab('cases')}), or change how Vigil works (${tab('workflows')}, ${tab('settings')}). A number on a tab means it has something for you.`,
    selector: 'nav[aria-label="Primary"]',
  },
  attention: {
    title: 'Decisions wait here',
    body: 'Needs your attention lists what Vigil stopped to ask you, oldest first. Approve or reject here, or open the case.',
    selector: 'section[aria-label="Needs your attention"]',
  },
  ask: {
    title: 'Ask Vigil anywhere',
    body: 'Ask about the page you are on or any case. Conversations are private to you and kept in your history.',
    selector: '.vg-command-ask',
  },
}

// The ask stop's ring hugs the command-bar button, as the board's glow does.
const PAD: Record<TourStopId, number> = { nav: 6, attention: 6, ask: 0 }
const CARD_W = 330
const CARD_H = 168 // first-paint guess; the rendered height replaces it
const GAP = 12

// floor: the bottom edge of the bar the target sits in, if any
function cardPosition(stop: TourStopId, rect: DOMRect | null, floor: number, cardH: number): { top: number; left: number } {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const left = rect && rect.width > 0
    ? Math.min(Math.max(12, rect.left), Math.max(12, vw - CARD_W - 12))
    : Math.max(12, vw - CARD_W - 24)
  if (!rect || rect.width === 0 || rect.height === 0) return { top: 72, left }
  const below = rect.bottom + GAP
  const above = rect.top - GAP - cardH
  switch (stop) {
    case 'nav':
    case 'attention':
      return { top: below + cardH < vh ? below : Math.max(12, above), left }
    // TourAsk: the card hangs just under the top bar, from the button
    case 'ask':
      return { top: Math.max(below, floor + 8), left }
    default: {
      const neverStop: never = stop
      return neverStop
    }
  }
}

export default function ConsoleTour({
  stops,
  index,
  onIndex,
  onDismiss,
}: {
  stops: readonly TourStopId[]
  index: number
  onIndex: (next: number) => void
  onDismiss: () => void
}) {
  const stop = stops[Math.min(index, stops.length - 1)] ?? 'nav'
  const copy = COPY[stop]
  const [rect, setRect] = useState<DOMRect | null>(null)
  const [floor, setFloor] = useState(0)
  const last = index >= stops.length - 1
  const cardRef = useRef<HTMLDivElement>(null)
  const [cardH, setCardH] = useState(CARD_H)

  // Copy length varies per stop, so place the card by its real height.
  useLayoutEffect(() => {
    const h = cardRef.current?.offsetHeight
    if (h && h !== cardH) setCardH(h)
  }, [stop, index, cardH])

  useLayoutEffect(() => {
    let frame = 0
    let raf = 0
    let cancelled = false
    let observer: ResizeObserver | null = null
    const measure = () => {
      const el = document.querySelector(copy.selector)
      setRect(el ? el.getBoundingClientRect() : null)
      setFloor(el?.closest('header')?.getBoundingClientRect().bottom ?? 0)
      return Boolean(el)
    }
    const watch = () => {
      if (observer) return
      observer = new ResizeObserver(measure)
      const screen = document.querySelector('.screen')
      const target = document.querySelector(copy.selector)
      if (screen) observer.observe(screen)
      if (target) observer.observe(target)
    }
    const tick = () => {
      if (cancelled) return
      const found = measure()
      if (found) watch()
      if (!found && frame < 8) {
        frame += 1
        raf = requestAnimationFrame(tick)
      }
    }
    tick()
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, true)
    return () => {
      cancelled = true
      cancelAnimationFrame(raf)
      observer?.disconnect()
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure, true)
    }
  }, [copy.selector])

  const card = cardPosition(stop, rect, floor, cardH)
  const pad = PAD[stop]

  return (
    <div className="console-tour" role="dialog" aria-modal="false" aria-labelledby="console-tour-title">
      {rect && rect.width > 0 && rect.height > 0 && (
        <div
          className="console-tour-ring"
          data-stop={stop}
          style={{
            top: rect.top - pad,
            left: rect.left - pad,
            width: rect.width + pad * 2,
            height: rect.height + pad * 2,
          }}
        />
      )}
      <div ref={cardRef} className="console-tour-card" style={{ top: card.top, left: card.left }}>
        <p className="console-tour-step">Step {index + 1} of {stops.length}</p>
        <h2 id="console-tour-title">{copy.title}</h2>
        <p>{copy.body}</p>
        <div className="console-tour-actions">
          <button type="button" className="console-tour-skip" onClick={onDismiss}>Skip tour</button>
          <span className="console-tour-spacer" />
          {index > 0 && <button type="button" className="btn" onClick={() => onIndex(index - 1)}>Back</button>}
          <button type="button" className="btn primary" onClick={() => (last ? onDismiss() : onIndex(index + 1))}>
            {last ? 'Done' : 'Next'}
          </button>
        </div>
      </div>
    </div>
  )
}
