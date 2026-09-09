import type { Finding } from './data'
import { MISSING_FINDING_TIME } from './data'
import { techniqueName } from './mitre'

export function findingTitle(finding: Pick<Finding, 'title' | 'tech' | 'src'>): string {
  if (finding.title?.trim()) return finding.title
  if (finding.tech !== '—') return techniqueName(finding.tech) || finding.tech
  return /flow|netflow/i.test(finding.src) ? 'Network flow finding' : 'Security finding'
}

export function sourceTimestamp(value?: string | null): number | undefined {
  if (!value) return undefined
  // The API also returns naive UTC datetimes; never interpret these as local time.
  const iso = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`
  const timestamp = Date.parse(iso)
  return Number.isFinite(timestamp) ? timestamp : undefined
}

export function sourceTime(value?: string | null): string {
  const timestamp = sourceTimestamp(value)
  return timestamp === undefined ? MISSING_FINDING_TIME : new Intl.DateTimeFormat('en-US', {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZone: 'UTC',
  }).format(timestamp) + ' UTC'
}

export function ipv4(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const parts = value.trim().split('.')
  return parts.length === 4 && parts.every((part) => /^(0|[1-9]\d{0,2})$/.test(part) && Number(part) <= 255)
    ? parts.join('.') : undefined
}

const SOURCE_KEYS = ['source_ip', 'source_ips', 'src_ip', 'src_ips', 'srcip', 'sourceIp']
const DESTINATION_KEYS = ['destination_ip', 'destination_ips', 'dest_ip', 'dest_ips', 'dst_ip', 'dst_ips', 'dstip', 'destinationIp']

/** Read only explicit role-bearing fields, before entity metadata is flattened.
 * Multiple distinct addresses or malformed aliases do not define a single flow.
 */
export function findingEndpoints(context: Record<string, unknown> | undefined): { sourceIp?: string; destinationIp?: string } {
  const one = (keys: string[]): string | undefined => {
    const values = keys.flatMap((key) => {
      const value = context?.[key]
      return value == null ? [] : Array.isArray(value) ? value : [value]
    })
    const normalized = values.map(ipv4)
    if (!normalized.length || normalized.some((value) => !value)) return undefined
    const unique = new Set(normalized)
    return unique.size === 1 ? normalized[0] : undefined
  }
  return { sourceIp: one(SOURCE_KEYS), destinationIp: one(DESTINATION_KEYS) }
}
