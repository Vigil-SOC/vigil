export const MONTHLY = '1M'
// the quota endpoint says "1mo", the key list "1M" (older keys "monthly")
const MONTHLY_FORMS = new Set([MONTHLY, '1mo', 'monthly', 'month'])
const PERIOD_NAMES: Record<string, string> = { '1d': 'daily', '1w': 'weekly', daily: 'daily', weekly: 'weekly' }

/** The reset period as a word, or null when it is monthly. */
export const periodBesideCeiling = (raw: string | undefined): string | null =>
  !raw || MONTHLY_FORMS.has(raw) ? null : (PERIOD_NAMES[raw] ?? raw)
