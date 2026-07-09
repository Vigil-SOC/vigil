/* ============================================================
   Extension registry helpers — fetch + validate a connector's
   manifest and resolve it into a RegisteredExtension. Pure
   functions (no React) so they're unit-testable.
   ============================================================ */
import {
  HOST_API_MAJOR,
  type ExtensionManifest,
  type RegisteredExtension,
} from './contracts'

/** Accept a manifest whose declared host-API major matches ours (e.g. this
 *  "1.x" host accepts "1", "1.0", "1.2.0"). Anything else is rejected so a
 *  future breaking host can refuse an old bundle rather than mis-mount it. */
export function isHostApiCompatible(declared: string): boolean {
  const major = parseInt(String(declared ?? '').split('.')[0], 10)
  return Number.isFinite(major) && major === HOST_API_MAJOR
}

/** Resolve a possibly-relative URL (e.g. "/assets/x.js") against the base. */
function resolveUrl(base: string, maybeRelative: string): string {
  try {
    return new URL(maybeRelative, base.endsWith('/') ? base : base + '/').toString()
  } catch {
    return maybeRelative
  }
}

/** Minimal structural validation so a malformed manifest is skipped rather
 *  than throwing deep inside the shell. Returns the manifest (unchanged) or
 *  null with a console warning. */
export function validateManifest(raw: unknown): ExtensionManifest | null {
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

/** Fetch + validate the manifest an integration's connector serves. Returns
 *  null (never throws) on any network/parse/validation failure so one broken
 *  connector can't take down the nav. */
export async function fetchManifest(
  integrationId: string,
  connectorUrl: string,
  signal?: AbortSignal,
): Promise<RegisteredExtension | null> {
  const base = connectorUrl.replace(/\/+$/, '')
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
  // resolve a possibly-relative bundleUrl against the connector base
  manifest.render = {
    ...manifest.render,
    bundleUrl: resolveUrl(base, manifest.render.bundleUrl),
  }
  return { integrationId, connectorUrl: base, manifest }
}
