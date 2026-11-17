import { Select } from './ui'

/** FalsePositiveCause in core/cases/closure.py. Absence is no value, not a member. */
export const FALSE_POSITIVE_CAUSES = [
  { value: 'expected', label: 'Expected activity' },
  { value: 'too_broad', label: 'Rule too broad' },
  { value: 'data', label: 'Data problem' },
  { value: 'other', label: 'Other' },
] as const

export type FalsePositiveCause = (typeof FALSE_POSITIVE_CAUSES)[number]['value']

const CAUSE_OPTIONS = [{ value: '', label: 'Not given' }, ...FALSE_POSITIVE_CAUSES]

/** Optional cause on a control that already dismisses. Nothing chosen sends no cause. */
export function CauseSelect({ value, onSelect }: {
  value: string
  onSelect: (value: string) => void
}) {
  return (
    <span className="cause-pick">
      <Select
        label="Cause, optional"
        value={value}
        placeholder="Cause, optional"
        onSelect={onSelect}
        options={CAUSE_OPTIONS}
      />
    </span>
  )
}
