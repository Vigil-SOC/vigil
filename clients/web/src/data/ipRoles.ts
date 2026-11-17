// Role of each address in a NetFlow finding, read from entity_context.netflow_digest
// (core/findings/netflow_digest.py): a pair's src is the client, its dst the server.

const SHOWN_PORTS = 3

type Pair = { src?: unknown; dst?: unknown; top_ports?: unknown; port_count?: unknown }

/** label per address (keyed lowercase): "client", or "server :80/443 +2". Addresses that are both, or not in the digest, get none. */
export function ipRoleLabels(ec: Record<string, unknown> | null | undefined): Map<string, string> {
  const out = new Map<string, string>()
  const digest = ec?.netflow_digest as { pairs?: unknown } | undefined
  const pairs = Array.isArray(digest?.pairs) ? (digest.pairs as Pair[]) : []
  const clients = new Set<string>()
  const servers = new Map<string, { ports: Map<number, number>; total: number }>()
  for (const p of pairs) {
    if (typeof p?.src === 'string') clients.add(p.src.toLowerCase())
    if (typeof p?.dst !== 'string') continue
    const key = p.dst.toLowerCase()
    const s = servers.get(key) ?? { ports: new Map<number, number>(), total: 0 }
    const top = Array.isArray(p.top_ports) ? p.top_ports : []
    for (const e of top) {
      if (Array.isArray(e) && typeof e[0] === 'number') s.ports.set(e[0], (s.ports.get(e[0]) ?? 0) + (typeof e[1] === 'number' ? e[1] : 0))
    }
    if (typeof p.port_count === 'number') s.total = Math.max(s.total, p.port_count)
    servers.set(key, s)
  }
  for (const c of clients) if (!servers.has(c)) out.set(c, 'client')
  for (const [ip, s] of servers) {
    if (clients.has(ip)) continue
    const ports = [...s.ports.entries()].sort((a, b) => b[1] - a[1]).map(([port]) => port)
    const shown = ports.slice(0, SHOWN_PORTS)
    const more = Math.max(s.total, ports.length) - shown.length
    let label = 'server'
    if (shown.length) label += ` :${shown.join('/')}`
    if (more > 0) label += ` +${more}`
    out.set(ip, label)
  }
  return out
}
