// What starts a workflow, from the `triggers` the API sends for each row.
// The scheduler runs on an interval (daily by default, set in the daemon config), not at night.
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
