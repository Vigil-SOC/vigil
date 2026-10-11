// Pure functions (no React), so they're unit-testable.
import {
  HOST_API_MAJOR,
  type ExtensionManifest,
  type RegisteredExtension,
} from './contracts'
import { extensionOriginAllowlist } from '../config/extensionAllowlist'

/** major only, so a future breaking host refuses an old bundle rather than
 *  mis-mounting it */
function isHostApiCompatible(declared: string): boolean {
  const major = parseInt(String(declared ?? '').split('.')[0], 10)
  return Number.isFinite(major) && major === HOST_API_MAJOR
}

function resolveUrl(base: string, maybeRelative: string): string {
  try {
    return new URL(maybeRelative, base.endsWith('/') ? base : base + '/').toString()
  } catch {
    return maybeRelative
  }
}

/** The bundle is imported as live code into Vigil's own origin, so an untrusted
 *  origin is arbitrary code execution: require https (http only for loopback)
 *  and, when configured, allowlist membership. The allowlist is shared with the
 *  backend CSP + SSRF guard (see config/extensionAllowlist.ts). */
function isTrustedConnectorUrl(raw: string): boolean {
  let u: URL
  try {
    u = new URL(raw)
  } catch {
    return false
  }
  const loopback =
    u.hostname === 'localhost' || u.hostname === '127.0.0.1' || u.hostname === '[::1]'
  if (u.protocol !== 'https:' && !(u.protocol === 'http:' && loopback)) return false
  const allow = extensionOriginAllowlist
  return allow.length === 0 || allow.includes(u.origin)
}

/** Fails closed on unparseable input. */
function sameOrigin(a: string, b: string): boolean {
  try {
    return new URL(a).origin === new URL(b).origin
  } catch {
    return false
  }
}

/** skipped rather than thrown, deep inside the shell */
function validateManifest(raw: unknown): ExtensionManifest | null {
  const m = raw as Partial<ExtensionManifest> | null
  if (!m || typeof m !== 'object') return null
  if (!isHostApiCompatible(String(m.hostApiVersion ?? ''))) {
    console.warn(
      `[extensions] skipping manifest "${m.id}": incompatible hostApiVersion "${m.hostApiVersion}"`,
    )
    return null
  }
  const r = m.render
  if (!r || r.mode !== 'element' || !r.bundleUrl || !r.elementTag) {
    console.warn(`[extensions] skipping manifest "${m.id}": invalid render block`)
    return null
  }
  if (!Array.isArray(m.mountPoints) || m.mountPoints.length === 0) {
    console.warn(`[extensions] skipping manifest "${m.id}": no mountPoints`)
    return null
  }
  return m as ExtensionManifest
}

/** never throws: one broken connector must not take down the nav */
export async function fetchManifest(
  integrationId: string,
  connectorUrl: string,
  signal?: AbortSignal,
): Promise<RegisteredExtension | null> {
  const base = connectorUrl.replace(/\/+$/, '')
  if (!isTrustedConnectorUrl(base)) {
    console.warn(
      `[extensions] refusing untrusted connector origin for "${integrationId}": ${base}`,
    )
    return null
  }
  let raw: unknown
  try {
    const res = await fetch(`${base}/manifest.json`, { signal, credentials: 'omit' })
    if (!res.ok) return null
    raw = await res.json()
  } catch (e) {
    if ((e as Error)?.name !== 'AbortError') {
      console.warn(`[extensions] manifest fetch failed for "${integrationId}":`, e)
    }
    return null
  }
  const manifest = validateManifest(raw)
  if (!manifest) return null
  // Resolve a possibly-relative bundleUrl, then hard-require it to share the
  // connector's origin — a manifest must not redirect the import() elsewhere.
  const bundleUrl = resolveUrl(base, manifest.render.bundleUrl)
  if (!sameOrigin(bundleUrl, base)) {
    console.warn(
      `[extensions] skipping manifest "${manifest.id}": bundleUrl origin does not match connector origin`,
    )
    return null
  }
  manifest.render = { ...manifest.render, bundleUrl }
  return { integrationId, connectorUrl: base, manifest }
}
