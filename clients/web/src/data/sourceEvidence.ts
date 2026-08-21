export type SourceTelemetryKind = 'netflow' | 'dns' | 'http_session' | 'generic_log'
export type SourceEvidenceStatus = 'available' | 'not_in_artifact' | 'redacted' | 'invalid'
export type SourceEvidenceProvenance = 'embedded' | 'joined'

export interface SourceEvidence {
  version: 1
  telemetryKind: SourceTelemetryKind
  schemaId: string
  status: SourceEvidenceStatus
  provenance: SourceEvidenceProvenance
  totalRecords: number
  truncated: boolean
  records: Array<Record<string, unknown>>
  rawText?: string
  rawTextTruncated: boolean
}

const KINDS = new Set<SourceTelemetryKind>(['netflow', 'dns', 'http_session', 'generic_log'])
const STATUSES = new Set<SourceEvidenceStatus>(['available', 'not_in_artifact', 'redacted', 'invalid'])
const PROVENANCE = new Set<SourceEvidenceProvenance>(['embedded', 'joined'])

function invalidEvidence(value?: Record<string, unknown>): SourceEvidence {
  const kind = KINDS.has(value?.telemetry_kind as SourceTelemetryKind)
    ? value?.telemetry_kind as SourceTelemetryKind
    : 'generic_log'
  return {
    version: 1,
    telemetryKind: kind,
    schemaId: typeof value?.schema_id === 'string' ? value.schema_id : 'unknown',
    status: 'invalid',
    provenance: 'embedded',
    totalRecords: 0,
    truncated: false,
    records: [],
    rawTextTruncated: false,
  }
}

function recordShapeIsValid(kind: SourceTelemetryKind, record: Record<string, unknown>): boolean {
  if (kind === 'netflow') {
    return typeof record.timestamp === 'string'
      && typeof record.source_ip === 'string'
      && typeof record.destination_ip === 'string'
  }
  if (kind === 'dns') return typeof record.query === 'string'
  return true
}

export function parseSourceEvidence(value: unknown): SourceEvidence | undefined {
  if (value === undefined || value === null) return undefined
  let parsed: unknown = value
  if (typeof value === 'string') {
    try {
      parsed = JSON.parse(value) as unknown
    } catch {
      return invalidEvidence()
    }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return invalidEvidence()
  const raw = parsed as Record<string, unknown>
  const kind = raw.telemetry_kind as SourceTelemetryKind
  const status = raw.status as SourceEvidenceStatus
  const provenance = raw.provenance as SourceEvidenceProvenance
  if (
    raw.version !== 1
    || !KINDS.has(kind)
    || typeof raw.schema_id !== 'string'
    || !STATUSES.has(status)
    || !PROVENANCE.has(provenance)
  ) return invalidEvidence(raw)

  if (status !== 'available') {
    return {
      version: 1,
      telemetryKind: kind,
      schemaId: raw.schema_id,
      status,
      provenance,
      totalRecords: 0,
      truncated: false,
      records: [],
      rawTextTruncated: false,
    }
  }

  const records = Array.isArray(raw.records)
    ? raw.records.filter((record): record is Record<string, unknown> => (
        Boolean(record) && typeof record === 'object' && !Array.isArray(record)
      ))
    : []
  if (Array.isArray(raw.records) && records.length !== raw.records.length) return invalidEvidence(raw)
  if (!records.every((record) => recordShapeIsValid(kind, record))) return invalidEvidence(raw)

  const rawText = typeof raw.raw_text === 'string' && raw.raw_text.trim() ? raw.raw_text : undefined
  if (records.length === 0 && !rawText) return invalidEvidence(raw)

  if (raw.total_records !== undefined && (
    typeof raw.total_records !== 'number' || !Number.isInteger(raw.total_records)
  )) return invalidEvidence(raw)
  if (raw.truncated !== undefined && typeof raw.truncated !== 'boolean') return invalidEvidence(raw)
  if (raw.raw_text_truncated !== undefined && typeof raw.raw_text_truncated !== 'boolean') return invalidEvidence(raw)
  const totalRecords = raw.total_records ?? records.length
  if (totalRecords < records.length || totalRecords < 0) return invalidEvidence(raw)

  return {
    version: 1,
    telemetryKind: kind,
    schemaId: raw.schema_id,
    status,
    provenance,
    totalRecords,
    truncated: raw.truncated === true || totalRecords > records.length,
    records,
    rawText,
    rawTextTruncated: raw.raw_text_truncated === true,
  }
}

export const SOURCE_TELEMETRY_LABELS: Record<SourceTelemetryKind, string> = {
  netflow: 'NetFlow',
  dns: 'DNS',
  http_session: 'HTTP session',
  generic_log: 'Log events',
}
