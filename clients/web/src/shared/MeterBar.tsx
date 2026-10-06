import type { Level } from './LevelBadge'
import './kit.css'

/** A thin used-of-limit bar. The colour is the server's level word, never derived from `pct`. */
export function MeterBar({ pct, level, label, className }: { pct: number; level: Level; label: string; className?: string }) {
  const used = Math.min(100, Math.max(0, Math.round(pct)))
  return (
    <span
      className={`meter-bar${level ? ` ${level}` : ''}${className ? ` ${className}` : ''}`}
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={used}
    >
      <span className="meter-fill" style={{ width: `${used}%` }} />
    </span>
  )
}
