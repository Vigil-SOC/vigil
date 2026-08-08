// Trusted page-extension connector origins, read at runtime from a
// <meta name="vigil-extension-allowlist"> tag (a <meta>, not inline <script>,
// because the CSP is script-src 'self'; mirrors config/basePath.ts). The same
// list drives the backend CSP + SSRF guard (services/extension_trust.py), so
// the browser trust gate can't drift into "trusted-here but CSP-blocked". Falls
// back to the deprecated build-time VITE_EXTENSION_ORIGIN_ALLOWLIST.
const _meta =
  (typeof document !== 'undefined' &&
    document
      .querySelector('meta[name="vigil-extension-allowlist"]')
      ?.getAttribute('content')) ||
  ''
const _fromVite =
  (import.meta.env.VITE_EXTENSION_ORIGIN_ALLOWLIST as string | undefined) ?? ''

export const extensionOriginAllowlist: string[] = (_meta || _fromVite)
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean)
