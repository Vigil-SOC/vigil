// The top-level entity_context keys the backend reads addresses from
// (core/storage/ip_exclusion_repository.py FINDING_IP_KEYS). Kept in step by hand:
// a key missing here only means the popup offers no Exclude button for it.
export const FINDING_IP_KEYS = [
  'src_ip', 'src_ips', 'source_ip', 'source_ips', 'srcip',
  'dst_ip', 'dst_ips', 'dest_ip', 'dest_ips', 'destination_ip', 'destination_ips', 'dstip',
  'ip', 'ips', 'ip_address', 'ip_addresses', 'local_ip', 'remote_ip', 'client_ip',
] as const

const IPV4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/
const IPV6ISH = /^[0-9a-f:.]+$/i

/** a loose shape check; the backend does the real validation */
export function looksLikeIp(value: string): boolean {
  const v = value.trim().replace(/^\[|\]$/g, '')
  return IPV4.test(v) || (v.includes(':') && IPV6ISH.test(v))
}

/** every address a finding names, deduped in first-seen order */
export function findingIps(ec: Record<string, unknown> | null | undefined): string[] {
  if (!ec) return []
  const seen = new Set<string>()
  const out: string[] = []
  for (const key of FINDING_IP_KEYS) {
    const raw = ec[key]
    const values = Array.isArray(raw) ? raw : [raw]
    for (const v of values) {
      if (typeof v !== 'string') continue
      const ip = v.trim()
      if (!ip || !looksLikeIp(ip) || seen.has(ip.toLowerCase())) continue
      seen.add(ip.toLowerCase())
      out.push(ip)
    }
  }
  return out
}
