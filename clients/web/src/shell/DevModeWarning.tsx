import { Icon } from '../shared/icons'

/**
 * The header item that says this console is not authenticated.
 *
 * With the bypass on, the SPA mocks a signed-in user, so there is no login
 * screen to notice and nothing else on the page looks any different from a
 * secured install. That is the whole problem: the state is invisible from the
 * inside.
 *
 * The build flag alone cannot answer the question. VITE_DEV_MODE is frozen into
 * the bundle, while DEV_MODE is read by the backend at runtime — a prebuilt
 * image deployed with the bypass on (values-dev.yaml) would show nothing, which
 * is the deployment where an outsider can reach it. So the shell renders this
 * when either says so (authBypassed in statusLine.ts).
 *
 * It lives in the header rather than as a bar across the page because a warning
 * that hides the thing you are working on gets removed, and a removed warning
 * warns nobody. The header is on screen from every view and costs the content
 * nothing.
 *
 * Not dismissible. A warning you can dismiss is one someone dismissed three
 * weeks ago.
 */

export default function DevModeWarning() {
  // tabIndex so the warning is reachable without a mouse.
  return (
    <div
      className="nav-btn dev-mode-warning"
      role="status"
      tabIndex={0}
      aria-label="Authentication bypassed: DEV_MODE is on"
      style={{
        // The same amber as --high, written out rather than referenced: a
        // re-theme must not be able to move a security warning.
        color: '#e0a44a',
        cursor: 'default',
      }}
    >
      <Icon name="alert" />
      <span className="nav-label" style={{ fontWeight: 600, letterSpacing: '0.02em' }}>
        NO AUTH
      </span>
      <span className="tip">
        DEV_MODE is on — authentication is bypassed. Every request is a
        full-permission admin.
      </span>
    </div>
  )
}
