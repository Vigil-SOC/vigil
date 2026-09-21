import { useEffect, useState } from 'react'
import { Icon } from '../shared/icons'
import { basePath } from '../config/basePath'

/**
 * The rail item that says this console is not authenticated.
 *
 * With the bypass on, the SPA mocks a signed-in user, so there is no login
 * screen to notice and nothing else on the page looks any different from a
 * secured install. That is the whole problem: the state is invisible from the
 * inside.
 *
 * The build flag alone cannot answer the question. VITE_DEV_MODE is frozen into
 * the bundle, while DEV_MODE is read by the backend at runtime — a prebuilt
 * image deployed with the bypass on (values-dev.yaml) would show nothing, which
 * is the deployment where an outsider can reach it. So the component asks the
 * backend as well, and warns if either says so.
 *
 * It lives in the rail rather than as a bar across the top because the top bar
 * overlays the console — a warning that hides the thing you are working on gets
 * removed, and a removed warning warns nobody. The rail is on screen from every
 * view and costs the content nothing.
 *
 * Not dismissible. A warning you can dismiss is one someone dismissed three
 * weeks ago.
 */

const BUILT_WITH_BYPASS = import.meta.env.VITE_DEV_MODE === 'true'

export default function DevModeWarning() {
  const [backendBypassed, setBackendBypassed] = useState(false)

  useEffect(() => {
    // A failed probe leaves the indicator as it is. It says "auth is off", and
    // a backend we cannot reach has told us nothing that justifies saying that.
    let live = true
    // Bounded: a backend that accepts the connection and never answers would
    // otherwise leave this request pending for the life of the session.
    fetch(`${basePath}/api/health`, {
      credentials: 'include',
      signal: AbortSignal.timeout(15_000),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        if (live && body?.auth_bypassed === true) setBackendBypassed(true)
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  if (!BUILT_WITH_BYPASS && !backendBypassed) return null

  // tabIndex so the tip is reachable without a mouse: with the rail collapsed
  // it is the only place the warning is spelled out.
  return (
    <div
      className="nav-btn dev-mode-warning"
      role="status"
      tabIndex={0}
      aria-label="Authentication bypassed: DEV_MODE is on"
      style={{
        // Not theme tokens: this must read the same under every theme, and it
        // should not blend into a palette someone has customised.
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
