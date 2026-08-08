/* Status pill for orchestrator investigation states. The shared
   .status pill only covers open/investigating/closed, so the
   autonomous-investigation states get their colours from tokens here. */

interface Meta {
  fg: string
  bg: string
  label?: string
}

const STATUS_META: Record<string, Meta> = {
  queued: { fg: 'var(--tx-3)', bg: 'var(--bg-2)' },
  assigned: { fg: 'var(--med)', bg: 'var(--med-dim)' },
  executing: { fg: 'var(--high)', bg: 'var(--high-dim)' },
  review_submitted: { fg: 'var(--high)', bg: 'var(--high-dim)', label: 'review' },
  completed: { fg: 'var(--ok)', bg: 'var(--ok-dim)' },
  failed: { fg: 'var(--crit)', bg: 'var(--crit-dim)' },
  sleeping: { fg: 'var(--tx-3)', bg: 'var(--bg-2)' },
  needs_rework: { fg: 'var(--high)', bg: 'var(--high-dim)', label: 'rework' },
}

const FALLBACK: Meta = { fg: 'var(--tx-3)', bg: 'var(--bg-2)' }

export function StatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] || FALLBACK
  const text = (meta.label || status).replace(/_/g, ' ')
  return (
    <span className="status" style={{ background: meta.bg, color: meta.fg }}>
      {text}
    </span>
  )
}
