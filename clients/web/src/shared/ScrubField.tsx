import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import './kit.css'

interface ScrubFieldProps {
  /** Accessible name, e.g. "Budget per case". */
  name: string
  /** Short label inside the field, e.g. "Budget". */
  label: string
  value: number
  min: number
  max: number
  step: number
  /** Text before the number ("$"). */
  prefix?: string
  /** Text after the number ("per case"). */
  unit?: string
  /** Called with a value inside [min, max] that differs from `value`. */
  onCommit: (value: number) => void
}

const DRAG_THRESHOLD = 3 // px; less than this is a click, which opens the typing field

const decimals = (step: number) => (String(step).split('.')[1] ?? '').length

/** Drag sideways, click to type, arrows step (Shift x10), Enter commits; bounds and step come from the caller. */
export function ScrubField({ name, label, value, min, max, step, prefix = '', unit, onCommit }: ScrubFieldProps) {
  const [drag, setDrag] = useState<number | null>(null) // the live value while dragging
  const [typing, setTyping] = useState<string | null>(null)
  const [shaking, setShaking] = useState(false) // set on every clamp; the animation clears it
  const start = useRef({ x: 0, from: 0, px: 6, moved: 0 })
  const root = useRef<HTMLSpanElement>(null)
  const closing = useRef(false) // the field already closed; the blur that follows must not commit again
  const byKey = useRef(false) // closed with Enter or Escape, so focus goes back to the field (not after a Tab or click away)

  useEffect(() => {
    if (typing === null && byKey.current) root.current?.focus()
    byKey.current = false
  }, [typing])

  const fix = (v: number) => Number(v.toFixed(decimals(step)))
  const clamp = (v: number) => Math.min(Math.max(v, min), max)
  const commit = (v: number) => {
    const next = fix(clamp(v))
    if (next !== v) setShaking(true)
    if (next !== value) onCommit(next)
  }

  const shown = drag ?? value
  const pct = max > min ? Math.min(Math.max((shown - min) / (max - min), 0), 1) * 100 : 0
  const text = `${prefix}${shown}`

  const onDown = (e: PointerEvent<HTMLSpanElement>) => {
    if (e.button !== 0) return
    const width = root.current?.getBoundingClientRect().width || 200
    // the full range takes about the field's width to cross, but never less than 2px or more than 12px a step
    const px = Math.min(Math.max(width / Math.max((max - min) / step, 1), 2), 12)
    start.current = { x: e.clientX, from: value, px, moved: 0 }
    e.currentTarget.setPointerCapture?.(e.pointerId)
    setDrag(value)
  }

  const onMove = (e: PointerEvent<HTMLSpanElement>) => {
    if (drag === null) return
    const dx = e.clientX - start.current.x
    start.current.moved = Math.max(start.current.moved, Math.abs(dx))
    if (start.current.moved < DRAG_THRESHOLD) return
    setDrag(fix(clamp(start.current.from + Math.trunc(dx / start.current.px) * step)))
  }

  const onUp = () => {
    if (drag === null) return
    const { moved } = start.current
    const v = drag
    setDrag(null)
    if (moved < DRAG_THRESHOLD) {
      closing.current = false
      setTyping(String(value))
    }
    else if (v !== value) commit(v)
  }

  const onKey = (e: KeyboardEvent<HTMLSpanElement>) => {
    const dir = e.key === 'ArrowRight' || e.key === 'ArrowUp' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowDown' ? -1 : 0
    if (dir) {
      e.preventDefault()
      const next = fix(value + dir * step * (e.shiftKey ? 10 : 1))
      const inRange = value >= min && value <= max
      // a step past a bound is refused; a saved value already outside it is pulled to the bound
      if (inRange && (next < min || next > max)) setShaking(true)
      else commit(next)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      closing.current = false
      setTyping(String(value))
    }
  }

  const settle = (raw: string) => {
    closing.current = true
    setTyping(null)
    const n = Number(raw)
    if (raw.trim() !== '' && Number.isFinite(n)) commit(n)
  }

  const body = (
    <>
      <span className="sf-label">{label}</span>
      <span className="sf-num">
        {typing === null ? (
          text
        ) : (
          <>
            {prefix}
            <input
              className="sf-input"
              aria-label={name}
              inputMode="decimal"
              autoComplete="off"
              autoFocus
              value={typing}
              onChange={(e) => setTyping(e.target.value)}
              onFocus={(e) => e.target.select()}
              onBlur={(e) => !closing.current && settle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  byKey.current = true
                  settle(e.currentTarget.value)
                } else if (e.key === 'Escape') {
                  byKey.current = true
                  closing.current = true
                  setTyping(null)
                }
                e.stopPropagation()
              }}
            />
          </>
        )}
      </span>
      {unit && <span className="sf-unit">{unit}</span>}
    </>
  )

  if (typing !== null) return <span className="sf typing">{body}</span>
  return (
    <span
      ref={root}
      role="spinbutton"
      tabIndex={0}
      aria-label={name}
      aria-valuenow={shown}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuetext={`${text}${unit ? ` ${unit}` : ''}`}
      title={`Drag sideways, or click to type. ${prefix}${min} to ${prefix}${max}.`}
      className={`sf${drag !== null ? ' dragging' : ''}${shaking ? ' shake' : ''}`}
      style={{ ['--sf-fill' as string]: `${pct}%` }}
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={() => setDrag(null)}
      onKeyDown={onKey}
      onAnimationEnd={() => setShaking(false)}
    >
      {body}
    </span>
  )
}
