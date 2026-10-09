export type CheckPhase = 'waiting' | 'checking' | 'passed' | 'needs'

const PHASE_LABEL: Record<CheckPhase, string> = {
  waiting: 'Waiting',
  checking: 'Checking',
  passed: 'Passed',
  needs: 'Needs you',
}

const FILLED = { fill: 'currentColor', fillOpacity: 0.14, stroke: 'currentColor', strokeWidth: 2 }
const GLYPH = { stroke: 'currentColor', strokeWidth: 2.2, strokeLinecap: 'round' as const }

/** The board's status mark: dashed ring, spinner, tick or "!". The label is the accessible name. */
export default function CheckMark({ phase }: { phase: CheckPhase }) {
  const color =
    phase === 'passed' ? 'var(--good)' : phase === 'needs' ? 'var(--fair)' : 'var(--tx-3)'
  return (
    <span
      role="img"
      aria-label={PHASE_LABEL[phase]}
      title={PHASE_LABEL[phase]}
      className="inline-flex w-[18px] h-[18px] shrink-0"
      style={{ color }}
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        {phase === 'waiting' && (
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" strokeDasharray="2.6 3.4" />
        )}
        {phase === 'checking' && (
          <>
            <circle cx="12" cy="12" r="9" stroke="var(--line)" strokeWidth="2" />
            <circle
              cx="12"
              cy="12"
              r="9"
              stroke="var(--accent)"
              strokeWidth="2.2"
              strokeLinecap="round"
              strokeDasharray="36 60"
              style={{
                transformBox: 'fill-box',
                transformOrigin: 'center',
                animation: 'vg-spin 1.1s linear infinite',
              }}
            />
          </>
        )}
        {phase === 'passed' && (
          <>
            <circle cx="12" cy="12" r="9" {...FILLED} />
            <path d="M7.8 12.4l2.9 2.9 5.6-5.8" {...GLYPH} strokeLinejoin="round" />
          </>
        )}
        {phase === 'needs' && (
          <>
            <circle cx="12" cy="12" r="9" {...FILLED} />
            <path d="M12 7.6v5.4M12 16.3v.1" {...GLYPH} strokeWidth={2.4} />
          </>
        )}
      </svg>
    </span>
  )
}
