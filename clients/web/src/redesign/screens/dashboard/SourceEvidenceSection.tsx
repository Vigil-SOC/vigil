import {
  SOURCE_TELEMETRY_LABELS,
  type LegacySourceEvidence,
  type SequenceSourceEvidence,
  type SourceEvidence,
} from '../../data/sourceEvidence'
import { findingsApi } from '../../../services/api'
import { useCallback, useEffect, useState, type ReactNode } from 'react'

const EMPTY = '—'

function displayValue(value: unknown): string {
  if (value === null) return 'null'
  if (value === undefined || value === '') return EMPTY
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function compactValue(value: unknown, maxLength = 120): string {
  const rendered = displayValue(value)
  return rendered.length > maxLength ? `${rendered.slice(0, maxLength - 1)}…` : rendered
}

function endpoint(ip: unknown, port: unknown): string {
  const address = displayValue(ip)
  return port === undefined || port === null || port === '' ? address : `${address}:${displayValue(port)}`
}

function TableRegion({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="source-evidence-table-scroll" role="region" aria-label={label} tabIndex={0}>
      {children}
    </div>
  )
}

function NetFlowTable({ records }: { records: Array<Record<string, unknown>> }) {
  return (
    <TableRegion label="NetFlow source evidence table">
      <table className="source-evidence-table">
        <caption className="sr-only">NetFlow records attached to this finding</caption>
        <thead><tr>
          <th scope="col">Time</th><th scope="col">Source</th><th scope="col">Destination</th>
          <th scope="col">Protocol</th><th scope="col">Packets F/B</th>
          <th scope="col">Bytes F/B</th><th scope="col">Duration</th>
        </tr></thead>
        <tbody>{records.map((record, index) => (
          <tr key={`${displayValue(record.timestamp)}-${index}`}>
            <td className="mono">{displayValue(record.timestamp)}</td>
            <td className="mono">{endpoint(record.source_ip, record.source_port)}</td>
            <td className="mono">{endpoint(record.destination_ip, record.destination_port)}</td>
            <td className="mono">{displayValue(record.protocol)}</td>
            <td className="mono">{displayValue(record.forward_packets)} / {displayValue(record.backward_packets)}</td>
            <td className="mono">{displayValue(record.forward_bytes)} / {displayValue(record.backward_bytes)}</td>
            <td className="mono">{record.duration_ms === undefined ? EMPTY : `${displayValue(record.duration_ms)} ms`}</td>
          </tr>
        ))}</tbody>
      </table>
    </TableRegion>
  )
}

function DnsTable({ evidence }: { evidence: LegacySourceEvidence }) {
  return (
    <TableRegion label="DNS source evidence table">
      <table className="source-evidence-table">
        <caption className="sr-only">DNS records attached to this finding</caption>
        <thead><tr>
          <th scope="col">Time</th><th scope="col">Client</th><th scope="col">Server</th>
          <th scope="col">Query</th><th scope="col">Type</th><th scope="col">Answer</th>
          <th scope="col">Rcode</th><th scope="col">TTL</th>
        </tr></thead>
        <tbody>{evidence.records.map((record, index) => (
          <tr key={`${displayValue(record.timestamp)}-${displayValue(record.query)}-${index}`}>
            <td className="mono">{displayValue(record.timestamp)}</td>
            <td className="mono">{displayValue(record.client_ip)}</td>
            <td className="mono">{displayValue(record.server_ip)}</td>
            <td className="mono">{displayValue(record.query)}</td>
            <td className="mono">{displayValue(record.query_type)}</td>
            <td className="mono">{displayValue(record.answer)}</td>
            <td className="mono">{displayValue(record.response_code)}</td>
            <td className="mono">{displayValue(record.ttl)}</td>
          </tr>
        ))}</tbody>
      </table>
    </TableRegion>
  )
}

function ModbusTable({ records }: { records: Array<Record<string, unknown>> }) {
  return (
    <TableRegion label="Modbus source evidence table">
      <table className="source-evidence-table">
        <caption className="sr-only">Packet-associated Modbus transactions in this sequence</caption>
        <thead><tr>
          <th scope="col">Time</th><th scope="col">Operation</th><th scope="col">Function</th>
          <th scope="col">Unit</th><th scope="col">Address / quantity</th><th scope="col">Response</th>
          <th scope="col">Values</th><th scope="col">Latency</th><th scope="col">Packets</th>
        </tr></thead>
        <tbody>{records.map((record, index) => (
          <tr key={`${displayValue(record.timestamp)}-${displayValue(record.transaction_id)}-${index}`}>
            <td className="mono">{displayValue(record.timestamp)}</td>
            <td>{displayValue(record.operation)}</td>
            <td>{displayValue(record.function_name ?? record.function_code)}</td>
            <td className="mono">{displayValue(record.unit_id)}</td>
            <td className="mono">
              {displayValue(record.address ?? record.write_address)} / {displayValue(record.quantity ?? record.write_quantity)}
            </td>
            <td>{displayValue(record.response_status)}</td>
            <td className="mono" title={displayValue(record.register_values ?? record.coil_values)}>
              {compactValue(record.register_values ?? record.coil_values)}
            </td>
            <td className="mono">{record.latency_usec === undefined ? EMPTY : `${displayValue(record.latency_usec)} µs`}</td>
            <td className="mono">{displayValue(record.request_packet)} / {displayValue(record.response_packet)}</td>
          </tr>
        ))}</tbody>
      </table>
    </TableRegion>
  )
}

function recordHeading(record: Record<string, unknown>, index: number): string {
  const parts = [record.timestamp, record.event_type, record.method, record.path, record.message]
    .filter((value) => typeof value === 'string' && value.trim())
    .map(String)
  return parts.join(' · ') || `Record ${index + 1}`
}

function StructuredRecords({ evidence }: { evidence: LegacySourceEvidence }) {
  return (
    <div className="source-evidence-records" aria-label="Structured source evidence records">
      {evidence.records.map((record, index) => (
        <details className="source-evidence-record" key={index}>
          <summary><span className="mono">{recordHeading(record, index)}</span></summary>
          <dl>
            {Object.entries(record).map(([key, value]) => (
              <div key={key}><dt>{key}</dt><dd className="mono">{displayValue(value)}</dd></div>
            ))}
          </dl>
        </details>
      ))}
    </div>
  )
}

const STATUS_MESSAGES: Record<Exclude<LegacySourceEvidence['status'], 'available'>, string> = {
  not_in_artifact: 'Source evidence was not included in the ingested artifact.',
  redacted: 'Source evidence is present but was redacted before ingestion.',
  invalid: 'Source evidence was present but did not match the declared schema.',
}

function SequenceStream({
  findingId,
  evidence,
  kind,
}: {
  findingId: string
  evidence: SequenceSourceEvidence
  kind: 'netflow' | 'modbus'
}) {
  const stream = evidence.streams[kind]
  const [offset, setOffset] = useState(0)
  const [records, setRecords] = useState(stream.records)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const pageSize = 100

  const load = useCallback(async (nextOffset: number) => {
    setLoading(true)
    setError('')
    try {
      const response = await findingsApi.getSourceEvidence(findingId, {
        kind,
        offset: nextOffset,
        limit: pageSize,
      })
      const page = response.data
      if (!page || !Array.isArray(page.records)) throw new Error('Invalid evidence page')
      setRecords(page.records)
      setOffset(nextOffset)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load evidence records')
    } finally {
      setLoading(false)
    }
  }, [findingId, kind])

  useEffect(() => {
    if (stream.totalRecords > 0 && stream.records.length === 0) void load(0)
  }, [load, stream.records.length, stream.totalRecords])

  if (stream.totalRecords === 0) {
    return kind === 'modbus' ? (
      <p className="source-evidence-caption">No packet-associated Modbus transactions occur in this sequence.</p>
    ) : null
  }
  return (
    <section aria-label={`${kind} sequence evidence`}>
      <h5>{kind === 'netflow' ? 'Normalized flows' : 'Modbus transactions'}</h5>
      {kind === 'netflow' ? <NetFlowTable records={records} /> : <ModbusTable records={records} />}
      <div className="source-evidence-pagination">
        <button className="btn ghost" disabled={loading || offset === 0} onClick={() => load(Math.max(0, offset - pageSize))}>Previous</button>
        <span className="mono">
          {offset + 1}–{Math.min(offset + records.length, stream.totalRecords)} of {stream.totalRecords}
        </span>
        <button className="btn ghost" disabled={loading || offset + records.length >= stream.totalRecords} onClick={() => load(offset + pageSize)}>Next</button>
      </div>
      {error && <p role="alert" className="source-evidence-caption">{error}</p>}
    </section>
  )
}

function SequenceEvidenceView({ findingId, evidence }: { findingId: string; evidence: SequenceSourceEvidence }) {
  const start = new Date(evidence.coverage.eventStartTime).toISOString()
  const end = new Date(evidence.coverage.eventEndTime).toISOString()
  return (
    <details className="modal-section source-evidence">
      <summary>
        <span>Source evidence</span>
        <span className="tag">Exact sequence</span>
        <span className="source-evidence-count">{evidence.summary.flowCount} flows</span>
      </summary>
      <p className="source-evidence-summary">{evidence.summary.text}</p>
      <p className="source-evidence-caption">
        Sequence <span className="mono">{evidence.sequenceId}</span>
        {' · '}{evidence.associationBasis === 'producer_membership' ? 'producer-declared membership' : 'validated sequence-builder replay'}
        {' · '}{start} to {end}
      </p>
      <p className="source-evidence-caption">
        Dataset <span className="mono">{evidence.datasetId}</span>
        {' · '}run <span className="mono">{evidence.runId}</span>
        {' · '}{evidence.coverage.captureSha256s.length} capture hash{evidence.coverage.captureSha256s.length === 1 ? '' : 'es'}
      </p>
      <details className="source-evidence-provenance">
        <summary>Provenance and integrity</summary>
        <dl>
          <div><dt>Evidence artifact</dt><dd className="mono">{evidence.artifact.artifactId}</dd></div>
          <div><dt>Flow SHA-256</dt><dd className="mono">{evidence.artifact.flowSha256}</dd></div>
          {evidence.artifact.modbusSha256 && (
            <div><dt>Modbus SHA-256</dt><dd className="mono">{evidence.artifact.modbusSha256}</dd></div>
          )}
          {evidence.coverage.captureSha256s.map((hash, index) => (
            <div key={hash}><dt>Capture SHA-256 {index + 1}</dt><dd className="mono">{hash}</dd></div>
          ))}
          <div><dt>Preview</dt><dd>
            {evidence.streams.netflow.truncated || evidence.streams.modbus.truncated
              ? 'Bounded previews; all records remain available through pagination.'
              : 'All records fit in the attached previews.'}
          </dd></div>
        </dl>
      </details>
      <SequenceStream key={`${evidence.artifact.artifactId}-netflow`} findingId={findingId} evidence={evidence} kind="netflow" />
      <SequenceStream key={`${evidence.artifact.artifactId}-modbus`} findingId={findingId} evidence={evidence} kind="modbus" />
    </details>
  )
}

export function SourceEvidenceSection({ evidence, findingId }: { evidence?: SourceEvidence; findingId: string }) {
  if (!evidence) return null
  if (evidence.version === 2) return <SequenceEvidenceView findingId={findingId} evidence={evidence} />
  const kindLabel = SOURCE_TELEMETRY_LABELS[evidence.telemetryKind]

  if (evidence.status !== 'available') {
    return (
      <section className="modal-section source-evidence-status" aria-label="Source evidence">
        <h4>Source evidence</h4>
        <p role="status"><strong>{kindLabel}:</strong> {STATUS_MESSAGES[evidence.status]}</p>
      </section>
    )
  }

  const recordCount = evidence.records.length
  const countLabel = evidence.totalRecords > 0
    ? `${recordCount} of ${evidence.totalRecords} records`
    : evidence.rawText ? 'Raw text' : 'No records'
  return (
    <details className="modal-section source-evidence">
      <summary>
        <span>Source evidence</span>
        <span className="tag">{kindLabel}</span>
        <span className="source-evidence-count">{countLabel}</span>
      </summary>
      <p className="source-evidence-caption">
        {evidence.provenance === 'embedded' ? 'Embedded in the ingested artifact' : 'Joined by the ingestion pipeline'}
        {' · '}schema <span className="mono">{evidence.schemaId}</span>
        {evidence.truncated ? ' · preview truncated' : ''}
      </p>
      {recordCount > 0 && evidence.telemetryKind === 'netflow' && <NetFlowTable records={evidence.records} />}
      {recordCount > 0 && evidence.telemetryKind === 'dns' && <DnsTable evidence={evidence} />}
      {recordCount > 0 && (evidence.telemetryKind === 'http_session' || evidence.telemetryKind === 'generic_log') && (
        <StructuredRecords evidence={evidence} />
      )}
      {evidence.rawText && (
        <div className="source-evidence-raw">
          <h5>Raw source text{evidence.rawTextTruncated ? ' (truncated)' : ''}</h5>
          <pre>{evidence.rawText}</pre>
        </div>
      )}
    </details>
  )
}
