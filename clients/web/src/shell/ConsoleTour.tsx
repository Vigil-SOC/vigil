import { useLayoutEffect, useState } from 'react'

export type TourStopId = 'nav' | 'attention' | 'ask'

const COPY: Record<TourStopId, { title: string; body: string; selector: string }> = {
  nav: {
    title: 'Primary nav',
    body: 'The primary nav switches between console screens.',
    selector: 'nav[aria-label="Primary"]',
  },
  attention: {
    title: 'Needs your attention',
    body: 'Needs your attention holds the decisions waiting on you.',
    selector: 'section[aria-label="Needs your attention"]',
  },
  ask: {
    title: 'Ask Vigil',
    body: 'Ask Vigil opens the chat assistant.',
    selector: '.chat-fab',
  },
}

const PAD = 6
const CARD_W = 300
const CARD_H = 168
const GAP = 12

function cardPosition(stop: TourStopId, rect: DOMRect | null): { top: number; left: number } {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const left = rect && rect.width > 0
    ? Math.min(Math.max(12, rect.left), Math.max(12, vw - CARD_W - 12))
    : Math.max(12, vw - CARD_W - 24)
  if (!rect || rect.width === 0 || rect.height === 0) return { top: 72, left }
  const below = rect.bottom + GAP
  const above = rect.top - GAP - CARD_H
  switch (stop) {
    case 'nav':
    case 'attention':
      return { top: below + CARD_H < vh ? below : Math.max(12, above), left }
    case 'ask':
      return { top: above > 12 ? above : below, left }
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
  const last = index >= stops.length - 1

  useLayoutEffect(() => {
    let frame = 0
    let raf = 0
    let cancelled = false
    let observer: ResizeObserver | null = null
    const measure = () => {
      const el = document.querySelector(copy.selector)
      setRect(el ? el.getBoundingClientRect() : null)
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

  const card = cardPosition(stop, rect)

  return (
    <div className="console-tour" role="dialog" aria-modal="false" aria-labelledby="console-tour-title">
      {rect && rect.width > 0 && rect.height > 0 && (
        <div
          className="console-tour-ring"
          data-stop={stop}
          style={{
            top: rect.top - PAD,
            left: rect.left - PAD,
            width: rect.width + PAD * 2,
            height: rect.height + PAD * 2,
          }}
        />
      )}
      <div className="console-tour-card" style={{ top: card.top, left: card.left }}>
        <p className="console-tour-step">{index + 1} of {stops.length}</p>
        <h2 id="console-tour-title">{copy.title}</h2>
        <p>{copy.body}</p>
        <div className="console-tour-actions">
          <button type="button" className="btn ghost" onClick={onDismiss}>Skip</button>
          <span className="console-tour-spacer" />
          <button type="button" className="btn" onClick={() => onIndex(index - 1)} disabled={index === 0}>Back</button>
          <button type="button" className="btn primary" onClick={() => (last ? onDismiss() : onIndex(index + 1))}>
            {last ? 'Done' : 'Next'}
          </button>
        </div>
      </div>
    </div>
  )
}
