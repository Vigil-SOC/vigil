// The scheduler's hunt runs on a configurable interval (daily by default), so the label names no time.
export const TRIGGER_LABEL: Record<string, string> = {
  alerts: 'On alerts',
  schedule: 'On a schedule',
  shadow: 'Runs alongside',
}

/** An absent list (older backend) gives nothing; an empty one means a person starts it. */
export function triggerLabels(triggers?: string[]): string[] {
  if (!triggers) return []
  return triggers.length ? triggers.map((t) => TRIGGER_LABEL[t] ?? t) : ['Started by hand']
}
