// Page-extension contracts (v1). The host owns these types; a connector conforms
// to them, and Vigil core knows nothing about any specific extension.

/** any extension whose declared major matches is accepted */
export const HOST_API_MAJOR = 1

/** Composed so it crosses the element's shadow boundary. */
export const EXTENSION_EVENT = 'vigil:extension'

interface ExtensionRender {
  mode: 'element'
  /** May be relative to the connector base; the registry resolves it absolute. */
  bundleUrl: string
  elementTag: string
}

interface ExtensionGate {
  /** only mount when this integration id is in the enabled-integrations list */
  integration?: string
}

export interface ExtensionMountPoint {
  type: 'screen'
  /** URL segment + registry key, e.g. "loglm" */
  key: string
  /** unknown names render blank rather than crashing */
  icon?: string
  navLabel: string
  title: string
  subtitle?: string
  permission?: string
  gate?: ExtensionGate
}

/** Source-chip branding for findings whose `data_source` == this manifest's id.
 *  Owned by the connector so no vendor colour is hardcoded host-side. */
interface ExtensionBadge {
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
