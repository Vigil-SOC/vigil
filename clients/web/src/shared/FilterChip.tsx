import './kit.css'

/** A pill that filters a list: the active one takes the accent border and tint. */
export function FilterChip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button type="button" className={`filter-chip${active ? ' active' : ''}`} aria-pressed={active} onClick={onClick}>
      {label}
    </button>
  )
}
