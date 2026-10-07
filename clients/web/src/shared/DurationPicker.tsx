import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { formatDuration, toMinutes } from './duration'
import { Icon } from './icons'
import './kit.css'

const MIN_TOTAL = 1 // a duration is at least a minute
const MAX_HOURS = 999
const MAX_TOTAL = MAX_HOURS * 60 + 59
const PRESETS = [
  { label: '15 min', minutes: 15 },
  { label: '30 min', minutes: 30 },
  { label: '1 h', minutes: 60 },
  { label: '4 h', minutes: 240 },
]

interface DurationPickerProps {
  /** Float hours, the unit the SLA fields store. */
  value: number
  onChange: (hours: number) => void
  /** Accessible name, e.g. "Respond within". */
  label: string
}

export function DurationPicker({ value, onChange, label }: DurationPickerProps) {
  const total = Math.min(Math.max(toMinutes(value), 0), MAX_TOTAL)
  const h = Math.floor(total / 60)
  const m = total % 60
  const [open, setOpen] = useState(false)
  const [shaking, setShaking] = useState(false) // set on every clamp; the animation clears it
  const [draft, setDraft] = useState<{ h: string; m: string } | null>(null)
  const root = useRef<HTMLSpanElement>(null)
  const pill = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    root.current?.querySelector('input')?.focus() // arrows work as soon as it opens
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const close = () => {
    setOpen(false)
    setDraft(null)
    pill.current?.focus()
  }

  // every edit lands as a total in minutes, clamped to the bounds
  const commit = (minutes: number) => {
    const clamped = Math.min(Math.max(minutes, MIN_TOTAL), MAX_TOTAL)
    if (clamped !== minutes) setShaking(true)
    setDraft(null)
    onChange(clamped / 60)
  }

  const type = (field: 'h' | 'm', raw: string) => {
    const digits = raw.replace(/\D/g, '').slice(0, 3)
    setDraft({ h: String(h), m: String(m), ...draft, [field]: digits })
    if (digits === '') return // mid-edit; the field settles on blur
    const n = Number(digits)
    const cap = field === 'h' ? MAX_HOURS : 59
    const next = Math.min(n, cap)
    if (next !== n) setShaking(true)
    const nextTotal = field === 'h' ? next * 60 + m : h * 60 + next
    if (nextTotal < MIN_TOTAL) {
      setShaking(true) // a zero-length SLA is refused, the draft stays on screen
      return
    }
    onChange(nextTotal / 60)
    setDraft((d) => (d ? { ...d, [field]: String(next) } : d))
  }

  const onKey = (field: 'h' | 'm') => (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      close()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      close()
    } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault()
      const step = field === 'h' ? 60 : 5
      commit(total + (e.key === 'ArrowUp' ? step : -step))
    }
  }

  const field = (key: 'h' | 'm', text: string, unit: string, name: string) => (
    <label className="dp-field">
      <input
        className="dp-input"
        inputMode="numeric"
        aria-label={name}
        value={text}
        onChange={(e) => type(key, e.target.value)}
        onKeyDown={onKey(key)}
        onFocus={(e) => e.target.select()}
        onBlur={() => setDraft(null)}
      />
      <span>{unit}</span>
    </label>
  )

  return (
    <span className="dp" ref={root}>
      <button
        ref={pill}
        type="button"
        className="dp-pill"
        aria-label={`${label}: ${formatDuration(total / 60)}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <b>{h}</b> h <b>{String(m).padStart(2, '0')}</b> min
        <Icon name="edit" size={12} />
      </button>
      {open && (
        <div role="dialog" aria-label={label} className="dp-pop">
          <div className={`dp-fields${shaking ? ' shake' : ''}`} onAnimationEnd={() => setShaking(false)}>
            {field('h', draft?.h ?? String(h), 'h', 'Hours')}
            {field('m', draft?.m ?? String(m), 'min', 'Minutes')}
          </div>
          <div className="dp-presets">
            {PRESETS.map((p) => (
              <button key={p.label} type="button" className="dp-preset" onClick={() => commit(p.minutes)}>
                {p.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </span>
  )
}
