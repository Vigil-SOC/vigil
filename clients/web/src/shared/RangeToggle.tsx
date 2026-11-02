import type { CountRange } from '../services/api'
import './kit.css'

const OPTIONS: { value: CountRange; label: string }[] = [
  { value: 'today', label: 'Today' },
  { value: 'all', label: 'All' },
]

/** The Today / All segmented control (Overview board, `ov.range`). */
export function RangeToggle({ range, onChange }: { range: CountRange; onChange: (next: CountRange) => void }) {
  return (
    <span className="range-seg" role="tablist" aria-label="Count over">
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          role="tab"
          aria-selected={range === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </span>
  )
}
