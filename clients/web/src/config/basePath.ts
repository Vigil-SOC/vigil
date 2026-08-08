// Context path (sub-path) the app is served under, e.g. "/vigilsoc" when behind
// a reverse proxy. Read at runtime from a <meta name="vigil-base-path"> tag that
// the backend injects into index.html (and the vite dev server injects in dev,
// from VIGIL_CONTEXT_PATH). A <meta> tag is used rather than an inline <script>
// because the app's CSP (script-src 'self') blocks inline scripts. Falls back to
// Vite's build-time BASE_URL. Empty string means served at the root.
// Consumed by the router basename, the axios baseURL, and absolute fetch calls.
const _meta =
  (typeof document !== 'undefined' &&
    document
      .querySelector('meta[name="vigil-base-path"]')
      ?.getAttribute('content')) ||
  ''
const _fromVite = (import.meta.env.BASE_URL || '').replace(/\/$/, '')
export const basePath: string = _meta || (_fromVite === '.' ? '' : _fromVite)
