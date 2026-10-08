export const toMinutes = (hours: number) => Math.round(hours * 60)

/** "2 hours", "1 hour 30 min", "45 min": the table's wording. */
export function formatDuration(hours: number): string {
  const total = toMinutes(hours)
  const h = Math.floor(total / 60)
  const m = total % 60
  const hh = h === 1 ? '1 hour' : `${h} hours`
  if (h === 0) return `${m} min`
  return m === 0 ? hh : `${hh} ${m} min`
}
