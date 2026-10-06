import './kit.css'

/** The server's good | fair | poor read; the client never derives it from a number. */
export type Level = 'good' | 'fair' | 'poor' | null

const WORD = { good: 'Good', fair: 'Fair', poor: 'Poor' } as const
const GLYPH = { good: 'M5 12.5l4.2 4.2L19 7.2', fair: 'M5 12h14', poor: 'M7 7l10 10M17 7 7 17' } as const

/** `word` is plain text ("—" when unmeasured); `pill` adds the tinted chip and glyph, and renders nothing when unmeasured. */
export function LevelBadge({ level, variant = 'word', className }: { level: Level; variant?: 'word' | 'pill'; className?: string }) {
  if (variant === 'word') return <span className={className}>{level ? WORD[level] : '—'}</span>
  if (!level) return null
  return (
    <span className={`level-pill ${level}${className ? ` ${className}` : ''}`}>
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" aria-hidden="true">
        <path d={GLYPH[level]} />
      </svg>
      {WORD[level]}
    </span>
  )
}
