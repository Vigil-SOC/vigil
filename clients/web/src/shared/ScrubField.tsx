import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Icon } from './icons'
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

const decimals = (step: number) => (String(step).split('.')[1] ?? '').length

/** A value that is clicked (or Enter) to type a new one; arrows step (Shift x10), Enter
 * commits. Neutral at rest: no fill or bar, since its place in the range means nothing to
 * read. Bounds and step come from the caller. */
export function ScrubField({ name, label, value, min, max, step, prefix = '', unit, onCommit }: ScrubFieldProps) {
  const [typing, setTyping] = useState<string | null>(null)
  const [shaking, setShaking] = useState(false) // set on every clamp; the animation clears it
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

  const text = `${prefix}${value}`
  const open = () => {
    closing.current = false
    setTyping(String(value))
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
      open()
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
      {typing === null && <Icon name="edit" size={12} className="sf-pen" />}
    </>
  )

  if (typing !== null) return <span className="sf typing">{body}</span>
  return (
    <span
      ref={root}
      role="spinbutton"
      tabIndex={0}
      aria-label={name}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuetext={`${text}${unit ? ` ${unit}` : ''}`}
      title={`Click to type. ${prefix}${min} to ${prefix}${max}.`}
      className={`sf${shaking ? ' shake' : ''}`}
      onClick={open}
      onKeyDown={onKey}
      onAnimationEnd={() => setShaking(false)}
    >
      {body}
    </span>
  )
}
