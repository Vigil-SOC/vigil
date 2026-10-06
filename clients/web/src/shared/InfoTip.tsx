import { useEffect, useRef, useState } from 'react'
import { Icon } from './icons'
import './kit.css'

interface InfoTipProps {
  /** Accessible name of the ⓘ button. */
  label: string
  /** The three lines of components/info-button.md; each is skipped when absent. */
  source?: string
  calculation?: string
  limit?: string
  /** A single sentence, for a mark that explains a state rather than a number. */
  text?: string
  /** Which edge of the ⓘ the popover lines up with. */
  align?: 'start' | 'end'
}

const LINES = [['source', 'Source'], ['calculation', 'Calculation'], ['limit', 'Limit']] as const

export function InfoTip({ label, text, align = 'end', ...lines }: InfoTipProps) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  return (
    <span
      ref={root}
      className="info-tip"
      onBlur={(e) => {
        if (!root.current?.contains(e.relatedTarget as Node | null)) setOpen(false)
      }}
      onKeyDown={(e) => e.key === 'Escape' && setOpen(false)}
    >
      <button type="button" aria-label={label} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Icon name="info" size={14} />
      </button>
      {open && (
        <span role="tooltip" className={`info-tip-pop ${align}`}>
          {text}
          {LINES.map(([key, name]) =>
            lines[key] ? (
              <span key={key} className="info-tip-line">
                <b>{name}</b> {lines[key]}
              </span>
            ) : null,
          )}
        </span>
      )}
    </span>
  )
}
