/* ============================================================
   Vigil page-extension contracts (v1) — the OSS host owns these,
   an add-on (e.g. the LogLM connector) conforms to them.

   Four contracts, all versioned under `hostApiVersion`:
     1. Manifest      — served by the connector, describes the page(s)
     2. Config        — stored by Vigil on the integration (connectorUrl)
     3. Host-context  — Vigil → element, set as the `hostContext` property
     4. Extension events — element → Vigil, one composed CustomEvent

   Vigil core carries only these types + the loading machinery; it knows
   nothing about any specific extension. The registry is empty by default.
   ============================================================ */

/** Host API version this Vigil build implements. Extensions declare the
 *  version they were built against; we accept anything on the same major
 *  (see `isHostApiCompatible`). */
export const HOST_API_VERSION = '1.x'
export const HOST_API_MAJOR = 1

/** name of the composed CustomEvent an element dispatches to talk to the host
 *  (bubbles + composed so it crosses the element's shadow boundary) */
export const EXTENSION_EVENT = 'vigil:extension'

// ---- 1. Manifest -------------------------------------------------------

export interface ExtensionRender {
  /** only 'element' (a custom element from an ES-module bundle) today */
  mode: 'element'
  /** URL of the ES-module bundle that defines the custom element. May be
   *  relative to the connector base; the registry resolves it absolute. */
  bundleUrl: string
  /** custom-element tag to instantiate once the bundle has loaded */
  elementTag: string
}

export interface ExtensionGate {
  /** only mount when this integration id is in the enabled-integrations list */
  integration?: string
}

export interface ExtensionMountPoint {
  /** only 'screen' (a top-level nav tab) today */
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

export interface ExtensionManifest {
  id: string
  name: string
  version: string
  hostApiVersion: string
  render: ExtensionRender
  mountPoints: ExtensionMountPoint[]
}

// ---- 2. Config (stored on the integration) -----------------------------
// { id, enabled, connectorUrl } — `connectorUrl` lives on the integration
// row; see frontend/src/config/integrations.ts (loglm entry).

/** A manifest resolved against the integration that supplied it. */
export interface RegisteredExtension {
  integrationId: string
  /** connector base URL (== host-context apiBase); resolves relative bundleUrl */
  connectorUrl: string
  manifest: ExtensionManifest
}

// ---- 3. Host-context (Vigil → element) ---------------------------------

export interface HostContext {
  /** the subset of Vigil theme tokens the element needs to look native */
  themeTokens: { '--accent': string; mode: 'light' | 'dark' }
  /** short-lived, user-scoped session token minted by the Vigil backend */
  session: { token: string; user: string }
  /** base URL the element calls directly (the connector BFF) */
  apiBase: string
}

/** An element accepts context via the `hostContext` JS property. */
export interface HostContextElement extends HTMLElement {
  hostContext?: HostContext
}

// ---- 4. Extension events (element → Vigil) -----------------------------

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
