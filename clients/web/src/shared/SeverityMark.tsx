import './kit.css'

const WORD = { critical: 'Critical', high: 'High', medium: 'Medium', low: 'Low' } as const

/** A case's priority as a coloured square and its word. */
export function SeverityMark({ level, className }: { level: string; className?: string }) {
  const known = level in WORD
  return (
    <span className={`sev-mark ${known ? level : 'unknown'}${className ? ` ${className}` : ''}`} title={known ? `${WORD[level as keyof typeof WORD]} priority` : 'Unknown priority'}>
      <span className="sev-box" aria-hidden="true" />
      {known ? WORD[level as keyof typeof WORD] : 'Unknown'}
    </span>
  )
}
