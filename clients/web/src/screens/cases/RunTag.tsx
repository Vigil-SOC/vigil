import type { Phase } from './useCases'

/** Names the run a row came from, in the Record's own meta type. */
export function RunTag({ label }: { label: string }) {
  return <span className="rec-run" title={label}>{label}</span>
}

/** A run still loading or unreadable on a tab that lists several: said under its own tag, so the other runs' rows stay. */
export function RunNotes({ runs, error = 'The run could not be read.' }: { runs: { id: string; label: string; phase: Phase }[]; error?: string }) {
  return (
    <>
      {runs.filter((run) => run.phase !== 'ready').map((run) => (
        <p key={run.id} className="muted case-run-line">
          <RunTag label={run.label} />
          {run.phase === 'loading' ? 'Loading the run…' : error}
        </p>
      ))}
    </>
  )
}
