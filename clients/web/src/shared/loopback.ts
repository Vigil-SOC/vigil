// Same host test the AI Config key form uses for an Ollama URL. A loopback
// host is this machine, so traffic to it does not leave the site.
const LOOPBACK_HOST = /^(https?:\/\/)?(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i

export function urlIsLoopback(url: string): boolean {
  return LOOPBACK_HOST.test(url.trim())
}

const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/

/** True when a URL's host is on this machine or the operator's own network:
    loopback, RFC 1918, link-local, host.docker.internal, a dotless name, or a
    `.local` / `.internal` name. A parse failure or empty string is false. */
export function urlIsPrivateHost(url: string): boolean {
  let host: string
  try {
    host = new URL(url.trim()).hostname.toLowerCase().replace(/\.$/, '')
  } catch {
    return false
  }
  if (!host) return false
  if (host.startsWith('[')) {
    // IPv6 literal: loopback or fe80::/10 link-local only
    const v6 = host.slice(1, -1)
    return v6 === '::1' || /^fe[89ab][0-9a-f]:/.test(v6)
  }
  const v4 = IPV4.exec(host)
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])]
    return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254)
  }
  return (
    host === 'localhost' ||
    host === 'host.docker.internal' ||
    !host.includes('.') ||
    host.endsWith('.local') ||
    host.endsWith('.internal')
  )
}
