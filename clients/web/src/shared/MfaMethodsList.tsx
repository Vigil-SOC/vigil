import type { Schema } from '../services/apiTypes'

export type MfaMethod = Schema<'MfaMethod'>

const day = (iso: string) => new Date(iso).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })
const stamp = (iso: string) =>
  new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

/**
 * A person's sign-in methods, read-only: name, when it was added and last used, and
 * the recovery codes left. The server sends no secret and no code.
 */
export default function MfaMethodsList({ methods }: { methods: MfaMethod[] }) {
  if (methods.length === 0) {
    return <div className="text-sm text-tx-3 py-6 text-center">No two-step sign-in methods.</div>
  }
  return (
    <ul className="flex flex-col gap-px m-0 p-0 list-none rounded-[10px] overflow-hidden" style={{ background: 'var(--line)' }}>
      {methods.map((m) => (
        <li key={m.type} className="px-3 py-2.5 bg-bg-2">
          <div className="text-sm text-tx">{m.name}</div>
          <div className="mt-0.5 text-xs text-tx-3 break-words">
            {[
              m.added_at ? `Added ${day(m.added_at)}` : 'Added date unknown',
              m.remaining != null ? `${m.remaining} left` : m.last_used_at ? `Last used ${stamp(m.last_used_at)}` : 'Not used yet',
            ].join(' · ')}
          </div>
        </li>
      ))}
    </ul>
  )
}
