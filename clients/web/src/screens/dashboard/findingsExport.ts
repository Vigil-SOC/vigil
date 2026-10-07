import type { Finding } from '../../data/data'

const FIXED_HEADERS = [
  'finding_id',
  'severity',
  'mitre_technique',
  'confidence_percent',
  'tactic',
  'source',
  'host',
  'user',
  'timestamp',
  'anomaly_score',
  'status',
]

function csvCell(value: string | number | null): string {
  // CSV escaping does not stop spreadsheet formula execution. Finding fields
  // can originate in attacker-controlled telemetry, so make formula-looking
  // strings literal before they reach Excel or similar tools.
  if (value == null) return ''
  const text = typeof value === 'string' && /^\s*[=+\-@]/.test(value)
    ? `'${value}`
    : String(value)
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function timestamp(finding: Finding): string {
  if (typeof finding.ts === 'number' && Number.isFinite(finding.ts)) {
    const date = new Date(finding.ts)
    if (!Number.isNaN(date.getTime())) return date.toISOString()
  }
  return ''
}

export function buildFindingsCsv(findings: Finding[]): string {
  const extraKeys = [...new Set(findings.flatMap((finding) => Object.keys(finding.extra ?? {})))].sort()
  const rows: (string | number | null)[][] = [
    [...FIXED_HEADERS, ...extraKeys],
    ...findings.map((finding) => [
      finding.id,
      finding.sev,
      finding.tech,
      finding.conf,
      finding.tactic,
      finding.src,
      finding.host,
      finding.user,
      timestamp(finding),
      finding.score,
      finding.status,
      ...extraKeys.map((key) => finding.extra?.[key] ?? ''),
    ]),
  ]
  return `${rows.map((row) => row.map(csvCell).join(',')).join('\n')}\n`
}

export function downloadFindingsCsv(findings: Finding[], now = new Date()): void {
  const csv = buildFindingsCsv(findings)
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `vigil-findings-${now.toISOString().slice(0, 10)}.csv`
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
