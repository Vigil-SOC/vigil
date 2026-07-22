import { SOURCE_TELEMETRY_LABELS, type SourceEvidence } from '../../data/sourceEvidence'
import type { ReactNode } from 'react'

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

function NetFlowTable({ evidence }: { evidence: SourceEvidence }) {
  return (
    <TableRegion label="NetFlow source evidence table">
      <table className="source-evidence-table">
        <caption className="sr-only">NetFlow records attached to this finding</caption>
        <thead><tr>
          <th scope="col">Time</th><th scope="col">Source</th><th scope="col">Destination</th>
          <th scope="col">Protocol</th><th scope="col">Packets F/B</th>
          <th scope="col">Bytes F/B</th><th scope="col">Duration</th>
        </tr></thead>
        <tbody>{evidence.records.map((record, index) => (
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

function DnsTable({ evidence }: { evidence: SourceEvidence }) {
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

function recordHeading(record: Record<string, unknown>, index: number): string {
  const parts = [record.timestamp, record.event_type, record.method, record.path, record.message]
    .filter((value) => typeof value === 'string' && value.trim())
    .map(String)
  return parts.join(' · ') || `Record ${index + 1}`
}

function StructuredRecords({ evidence }: { evidence: SourceEvidence }) {
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

const STATUS_MESSAGES: Record<Exclude<SourceEvidence['status'], 'available'>, string> = {
  not_in_artifact: 'Source evidence was not included in the ingested artifact.',
  redacted: 'Source evidence is present but was redacted before ingestion.',
  invalid: 'Source evidence was present but did not match the declared schema.',
}

export function SourceEvidenceSection({ evidence }: { evidence?: SourceEvidence }) {
  if (!evidence) return null
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
      {recordCount > 0 && evidence.telemetryKind === 'netflow' && <NetFlowTable evidence={evidence} />}
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
