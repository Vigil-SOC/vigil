export const FINDINGS_VIEW_STORAGE_KEY = 'soc.findings.filters.v1'

type FindingsSeverityFilter = 'any' | 'critical' | 'high' | 'medium' | 'low'

interface FindingsViewPreferences {
  severity: FindingsSeverityFilter
  source: string
  hiddenColumns: string[] | null
}

export const DEFAULT_FINDINGS_VIEW_PREFERENCES: FindingsViewPreferences = {
  severity: 'any',
  source: 'any',
  hiddenColumns: null,
}

const SEVERITIES = new Set<FindingsSeverityFilter>([
  'any',
  'critical',
  'high',
  'medium',
  'low',
])

export function isFindingsSeverityFilter(value: unknown): value is FindingsSeverityFilter {
  return typeof value === 'string' && SEVERITIES.has(value as FindingsSeverityFilter)
}

function safeStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function parsePreferences(raw: string | null): FindingsViewPreferences {
  if (!raw) return DEFAULT_FINDINGS_VIEW_PREFERENCES
  try {
    const value = JSON.parse(raw) as Record<string, unknown>
    const severity = isFindingsSeverityFilter(value.severity)
      ? value.severity
      : 'any'
    const source = typeof value.source === 'string' && value.source.length > 0 && value.source.length <= 256
      ? value.source
      : 'any'
    const hiddenColumns = value.hiddenColumns === null
      ? null
      : Array.isArray(value.hiddenColumns)
        ? [...new Set(value.hiddenColumns.filter((key): key is string => typeof key === 'string' && key.length > 0 && key.length <= 256))].slice(0, 128)
        : null
    return { severity, source, hiddenColumns }
  } catch {
    return DEFAULT_FINDINGS_VIEW_PREFERENCES
  }
}

export function loadFindingsViewPreferences(storage: Storage | null = safeStorage()): FindingsViewPreferences {
  if (!storage) return DEFAULT_FINDINGS_VIEW_PREFERENCES
  try {
    return parsePreferences(storage.getItem(FINDINGS_VIEW_STORAGE_KEY))
  } catch {
    return DEFAULT_FINDINGS_VIEW_PREFERENCES
  }
}

export function saveFindingsViewPreferences(
  preferences: FindingsViewPreferences,
  storage: Storage | null = safeStorage(),
): boolean {
  if (!storage) return false
  try {
    storage.setItem(FINDINGS_VIEW_STORAGE_KEY, JSON.stringify(preferences))
    return true
  } catch {
    return false
  }
}
