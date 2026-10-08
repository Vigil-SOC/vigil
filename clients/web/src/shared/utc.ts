/* Time is UTC, 24-hour, "14:22" (DESIGN.md §2). One basis for Watch a run and the case page. */
const DAY = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric', year: 'numeric' })

/** "14:22" from an ISO stamp; null when it is missing or does not parse. */
export function utcClock(iso?: string | null): string | null {
  const t = iso ? new Date(iso) : null
  return t && !Number.isNaN(t.getTime()) ? t.toISOString().slice(11, 16) : null
}

/** "Jun 15, 2026"; null when the stamp is missing or does not parse. */
export function utcDay(iso?: string | null): string | null {
  const t = iso ? new Date(iso) : null
  return t && !Number.isNaN(t.getTime()) ? DAY.format(t) : null
}

/** "Jun 15, 2026 · 14:22"; null when the stamp is missing or does not parse. */
export function utcDayClock(iso?: string | null): string | null {
  const day = utcDay(iso)
  return day && `${day} · ${utcClock(iso)}`
}
