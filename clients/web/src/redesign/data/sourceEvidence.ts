export type SourceTelemetryKind = 'netflow' | 'dns' | 'http_session' | 'generic_log'
export type SourceEvidenceStatus = 'available' | 'not_in_artifact' | 'redacted' | 'invalid'
export type SourceEvidenceProvenance = 'embedded' | 'joined'

export interface LegacySourceEvidence {
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

export interface SequenceEvidenceStream {
  schemaId: string
  totalRecords: number
  truncated: boolean
  records: Array<Record<string, unknown>>
}

export interface SequenceSourceEvidence {
  version: 2
  status: 'available'
  provenance: 'joined'
  telemetryKind: 'netflow'
  schemaId: 'sequence_evidence/v1'
  totalRecords: number
  truncated: boolean
  records: Array<Record<string, unknown>>
  rawTextTruncated: false
  associationBasis: 'producer_membership' | 'sequence_builder_replay'
  sequenceId: string
  datasetId: string
  runId: string
  artifact: {
    artifactId: string
    flowSha256: string
    modbusSha256?: string
  }
  summary: {
    text: string
    flowCount: number
    packetCount: number
    byteCount: number
    protocolCounts: Record<string, number>
    modbusTransactionCount: number
    modbusOperationCounts: Record<string, number>
    modbusResponseCounts: Record<string, number>
  }
  coverage: {
    eventStartTime: number
    eventEndTime: number
    captureSha256s: string[]
    flowMembershipComplete: boolean
    modbusPacketAssociation: boolean
  }
  streams: {
    netflow: SequenceEvidenceStream
    modbus: SequenceEvidenceStream
  }
}

export type SourceEvidence = LegacySourceEvidence | SequenceSourceEvidence

export interface SourceEvidencePage {
  kind: 'netflow' | 'modbus'
  total: number
  offset: number
  limit: number
  records: Array<Record<string, unknown>>
}

const KINDS = new Set<SourceTelemetryKind>(['netflow', 'dns', 'http_session', 'generic_log'])
const STATUSES = new Set<SourceEvidenceStatus>(['available', 'not_in_artifact', 'redacted', 'invalid'])
const PROVENANCE = new Set<SourceEvidenceProvenance>(['embedded', 'joined'])
const SHA256 = /^[a-f0-9]{64}$/

function invalidEvidence(value?: Record<string, unknown>): LegacySourceEvidence {
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
  if (raw.version === 2) return parseSequenceEvidence(raw)
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

function objectRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function numberRecord(value: unknown): Record<string, number> | undefined {
  if (!objectRecord(value)) return undefined
  const entries = Object.entries(value)
  if (!entries.every(([, count]) => typeof count === 'number' && Number.isFinite(count))) return undefined
  return Object.fromEntries(entries) as Record<string, number>
}

function sequenceStream(value: unknown, schemaId: string): SequenceEvidenceStream | undefined {
  if (!objectRecord(value) || value.schema_id !== schemaId) return undefined
  const total = value.total_records
  const truncated = value.truncated
  const records = value.records === undefined
    ? []
    : Array.isArray(value.records) && value.records.every(objectRecord)
      ? value.records
      : undefined
  if (
    typeof total !== 'number'
    || !Number.isInteger(total)
    || total < 0
    || typeof truncated !== 'boolean'
    || !records
    || total < records.length
  ) return undefined
  return { schemaId, totalRecords: total, truncated: truncated || total > records.length, records }
}

function parseSequenceEvidence(raw: Record<string, unknown>): SourceEvidence {
  const artifact = objectRecord(raw.artifact) ? raw.artifact : undefined
  const summary = objectRecord(raw.summary) ? raw.summary : undefined
  const coverage = objectRecord(raw.coverage) ? raw.coverage : undefined
  const streams = objectRecord(raw.streams) ? raw.streams : undefined
  const netflow = sequenceStream(streams?.netflow, 'sequence_evidence/v1')
  const modbus = sequenceStream(streams?.modbus, 'sequence_protocol_evidence/v1')
  const protocolCounts = numberRecord(summary?.protocol_counts)
  const operationCounts = numberRecord(summary?.modbus_operation_counts)
  const responseCounts = numberRecord(summary?.modbus_response_counts)
  const association = raw.association_basis
  const captureHashes = coverage?.capture_sha256s
  if (
    raw.status !== 'available'
    || raw.provenance !== 'joined'
    || (association !== 'producer_membership' && association !== 'sequence_builder_replay')
    || typeof raw.sequence_id !== 'string' || !raw.sequence_id.trim()
    || typeof raw.dataset_id !== 'string' || !raw.dataset_id.trim()
    || typeof raw.run_id !== 'string' || !raw.run_id.trim()
    || !artifact
    || typeof artifact.artifact_id !== 'string' || !SHA256.test(artifact.artifact_id)
    || typeof artifact.flow_sha256 !== 'string' || !SHA256.test(artifact.flow_sha256)
    || (artifact.modbus_sha256 !== null && artifact.modbus_sha256 !== undefined
      && (typeof artifact.modbus_sha256 !== 'string' || !SHA256.test(artifact.modbus_sha256)))
    || !summary
    || typeof summary.text !== 'string'
    || typeof summary.flow_count !== 'number'
    || typeof summary.packet_count !== 'number'
    || typeof summary.byte_count !== 'number'
    || typeof summary.modbus_transaction_count !== 'number'
    || !protocolCounts
    || !operationCounts
    || !responseCounts
    || !coverage
    || typeof coverage.event_start_time !== 'number'
    || typeof coverage.event_end_time !== 'number'
    || !Array.isArray(captureHashes)
    || !captureHashes.every((hash) => typeof hash === 'string')
    || typeof coverage.flow_membership_complete !== 'boolean'
    || typeof coverage.modbus_packet_association !== 'boolean'
    || !netflow
    || !modbus
  ) return invalidEvidence(raw)

  return {
    version: 2,
    status: 'available',
    provenance: 'joined',
    telemetryKind: 'netflow',
    schemaId: 'sequence_evidence/v1',
    totalRecords: netflow.totalRecords,
    truncated: netflow.truncated,
    records: netflow.records,
    rawTextTruncated: false,
    associationBasis: association,
    sequenceId: raw.sequence_id,
    datasetId: raw.dataset_id,
    runId: raw.run_id,
    artifact: {
      artifactId: artifact.artifact_id,
      flowSha256: artifact.flow_sha256,
      modbusSha256: typeof artifact.modbus_sha256 === 'string' ? artifact.modbus_sha256 : undefined,
    },
    summary: {
      text: summary.text,
      flowCount: summary.flow_count,
      packetCount: summary.packet_count,
      byteCount: summary.byte_count,
      protocolCounts,
      modbusTransactionCount: summary.modbus_transaction_count,
      modbusOperationCounts: operationCounts,
      modbusResponseCounts: responseCounts,
    },
    coverage: {
      eventStartTime: coverage.event_start_time,
      eventEndTime: coverage.event_end_time,
      captureSha256s: captureHashes as string[],
      flowMembershipComplete: coverage.flow_membership_complete,
      modbusPacketAssociation: coverage.modbus_packet_association,
    },
    streams: { netflow, modbus },
  }
}

export const SOURCE_TELEMETRY_LABELS: Record<SourceTelemetryKind, string> = {
  netflow: 'NetFlow',
  dns: 'DNS',
  http_session: 'HTTP session',
  generic_log: 'Log events',
}
