import type { Level } from './LevelBadge'
import './kit.css'

/** A 5px usage track. `level` comes from the server's read (never derived from `pct`); `pct` only sets the width. */
export function MeterBar({ pct, level, label }: { pct: number; level: Level; label: string }) {
  const width = Number.isFinite(pct) ? Math.min(100, Math.max(0, pct)) : 0
  return (
    <div className="meter-bar" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(width)}>
      {level && <div className={`meter-fill ${level}`} style={{ width: `${width}%` }} />}
    </div>
  )
}
