// Context path (sub-path) the app is served under, e.g. "/vigil" when behind a
// reverse proxy. Resolved at runtime from window.__VIGIL_BASE_PATH__ (injected
// into index.html by the backend, which reads VIGIL_CONTEXT_PATH), falling back
// to Vite's build-time BASE_URL. Empty string means served at the root.
// Consumed by the router basename, the axios baseURL, and absolute fetch calls.
const _fromVite = (import.meta.env.BASE_URL || '').replace(/\/$/, '')
export const basePath: string =
  (window as any).__VIGIL_BASE_PATH__ || (_fromVite === '.' ? '' : _fromVite)
