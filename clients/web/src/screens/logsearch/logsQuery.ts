// Logs search: the query language as the server reads it, time ranges, and CSV.
import type { LogRow } from '../../services/api'

export interface Term { key: string | null; value: string; negate: boolean; op?: string }

export const KEYS = ['source', 'host', 'ip', 'user', 'port', 'incident', 'mitre', 'sev', 'class', 'action', 'id']

/** Read a search the way the server does: "quoted phrases", key:value, -exclude, key:a,b, loglm>0.7. */
export function parseQuery(q: string): Term[] {
  const out: Term[] = []
  const re = /(-?)(?:([a-z]+):)?(?:"([^"]*)"|(\S+))/gi
  for (const m of q.matchAll(re)) {
    const negate = m[1] === '-'
    const raw = m[3] ?? m[4] ?? ''
    const key = m[2]?.toLowerCase() ?? null
    if (!key) {
      const score = raw.match(/^loglm([<>]=?)([\d.]+)$/i)
      if (score) { out.push({ key: 'loglm', value: score[2], negate, op: score[1] }); continue }
    }
    if (!raw) continue
    if (key && !KEYS.includes(key)) out.push({ key: null, value: `${key}:${raw}`, negate })
    else out.push({ key, value: raw, negate })
  }
  return out
}

/** Write terms back as a search, so one filter can be taken out. */
export function formatQuery(terms: Term[]): string {
  return terms
    .map((t) => {
      if (t.key === 'loglm') return `${t.negate ? '-' : ''}loglm${t.op ?? '>='}${t.value}`
      const v = /\s/.test(t.value) ? `"${t.value}"` : t.value
      return `${t.negate ? '-' : ''}${t.key ? `${t.key}:` : ''}${v}`
    })
    .join(' ')
}

const KEY_WORD: Record<string, string> = { source: 'source', host: 'host', ip: 'IP', user: 'user', port: 'port', incident: 'case', mitre: 'technique', sev: 'severity', class: 'class', action: 'action', id: 'event' }

/** "host is web-01", "not entra", "unusual ≥ 0.9": a term as a person reads it. */
export function termLabel(t: Term): string {
  if (t.key === 'loglm') return `${t.negate ? 'not ' : ''}unusual ${t.op === '>' ? '>' : t.op === '<' ? '<' : t.op === '<=' ? '≤' : '≥'} ${t.value}`
  if (t.key === 'incident' && t.value === '*') return t.negate ? 'not tied to a case' : 'tied to a case'
  if (!t.key) {
    const v = t.value.includes(',') && !/\s/.test(t.value) ? t.value.split(',').filter(Boolean).map((x) => `“${x}”`).join(' or ') : `“${t.value}”`
    return t.negate ? `without ${v}` : v
  }
  const v = t.value.split(',').join(' or ')
  return `${KEY_WORD[t.key]} ${t.negate ? 'is not' : 'is'} ${v}`
}

// ── Time ranges ──────────────────────────────────────────────────────────────

export type RangeKey = '15m' | '1h' | '4h' | '24h' | '7d' | 'custom'
export interface Range { key: RangeKey; start?: number; end?: number }

export const MIN = 60_000
export const DAY = 1440 * MIN
export const RANGES: { key: RangeKey; label: string; ms?: number }[] = [
  { key: '15m', label: 'Last 15 minutes', ms: 15 * MIN },
  { key: '1h', label: 'Last hour', ms: 60 * MIN },
  { key: '4h', label: 'Last 4 hours', ms: 240 * MIN },
  { key: '24h', label: 'Last 24 hours', ms: DAY },
  { key: '7d', label: 'Last 7 days', ms: 7 * DAY },
  { key: 'custom', label: 'Custom' },
]

export const isRelative = (r: Range) => r.key !== 'custom'

/** The range as start and end times, now. */
export function windowOf(r: Range, now = Date.now()): [number, number] {
  if (r.key === 'custom') return [r.start ?? now - DAY, r.end ?? now]
  const ms = RANGES.find((x) => x.key === r.key)?.ms ?? DAY
  return [now - ms, now]
}

export function rangeFromParam(v: string | null): Range {
  if (!v) return { key: '24h' }
  const custom = v.match(/^(\d+)-(\d+)$/)
  if (custom) return { key: 'custom', start: Number(custom[1]), end: Number(custom[2]) }
  return RANGES.some((r) => r.key === v) ? { key: v as RangeKey } : { key: '24h' }
}

export const rangeToParam = (r: Range): string => (r.key === 'custom' ? `${r.start}-${r.end}` : r.key)

// ── Sources ──────────────────────────────────────────────────────────────────

// Names for the source keys senders commonly use; any other key shows as sent.
const SOURCE_NAMES: Record<string, string> = {
  sysmon: 'Sysmon',
  zeek: 'Zeek',
  paloalto: 'Palo Alto',
  dns: 'DNS',
  winsec: 'Windows Security',
  m365: 'Microsoft 365',
  crowdstrike: 'CrowdStrike',
  entra: 'Entra ID',
  okta: 'Okta',
  cloudtrail: 'CloudTrail',
}

export const sourceName = (key: string) => SOURCE_NAMES[key] ?? key

// ── Export ───────────────────────────────────────────────────────────────────

/** The loaded rows as CSV, for a spreadsheet or a ticket. */
export function toCsv(rows: LogRow[]): string {
  const cols: (keyof LogRow)[] = ['time', 'source', 'class', 'severity', 'srcIp', 'srcHost', 'dstIp', 'dstHost', 'dstPort', 'user', 'action', 'summary', 'loglm', 'mitre', 'incident', 'id']
  const cell = (v: unknown) => {
    const s = v === null || v === undefined ? '' : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  return [cols.join(','), ...rows.map((r) => cols.map((c) => cell(r[c])).join(','))].join('\n')
}
