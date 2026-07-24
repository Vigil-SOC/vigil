// Vigil page-extension contracts (v1): the OSS host owns these types; a
// connector (e.g. LogLM) conforms to them. Vigil core knows nothing about any
// specific extension. Four contracts, versioned under `hostApiVersion`:
// Manifest (connector → host), Config (stored on the integration), Host-context
// (host → element via `hostContext`), and Extension events (element → host).

/** We accept any extension whose declared major matches (see isHostApiCompatible). */
export const HOST_API_VERSION = '1.x'
export const HOST_API_MAJOR = 1

/** Composed so it crosses the element's shadow boundary. */
export const EXTENSION_EVENT = 'vigil:extension'

export interface ExtensionRender {
  mode: 'element'
  /** May be relative to the connector base; the registry resolves it absolute. */
  bundleUrl: string
  elementTag: string
}

export interface ExtensionGate {
  /** only mount when this integration id is in the enabled-integrations list */
  integration?: string
}

export interface ExtensionMountPoint {
  type: 'screen'
  /** URL segment + registry key, e.g. "loglm" */
  key: string
  /** nav-rail icon name; unknown names render blank (never crash) */
  icon?: string
  navLabel: string
  title: string
  subtitle?: string
  /** RBAC permission required to see/open this page (honored by the host) */
  permission?: string
  gate?: ExtensionGate
}

/** Source-chip branding for findings whose `data_source` == this manifest's id.
 *  Owned by the connector so no vendor colour/icon is hardcoded host-side. */
export interface ExtensionBadge {
  label?: string
  color?: string
  /** host IconName; unknown names fall back to neutral */
  icon?: string
}

export interface ExtensionManifest {
  id: string
  name: string
  version: string
  hostApiVersion: string
  badge?: ExtensionBadge
  render: ExtensionRender
  mountPoints: ExtensionMountPoint[]
}

/** A manifest resolved against the integration that supplied it. */
export interface RegisteredExtension {
  integrationId: string
  /** connector base URL (== host-context apiBase); resolves relative bundleUrl */
  connectorUrl: string
  manifest: ExtensionManifest
}

export interface HostContext {
  themeTokens: { '--accent': string; mode: 'light' | 'dark' }
  /** absent when the connector runs without auth (no mint secret configured) */
  session?: { token: string; user: string }
  /** base URL the element calls directly (the connector BFF) */
  apiBase: string
}

export interface HostContextElement extends HTMLElement {
  hostContext?: HostContext
}

export type ExtensionEvent =
  | { type: 'ready' }
  | { type: 'navigate'; payload: { to: string } }
  | {
      type: 'notify'
      payload: { severity: 'info' | 'warn' | 'error' | 'success'; message: string }
    }
  | { type: 'setViewFull'; payload: { full: boolean } }
  | { type: 'requestContextRefresh' }
  | { type: 'error'; payload?: { message?: string; detail?: unknown } }
