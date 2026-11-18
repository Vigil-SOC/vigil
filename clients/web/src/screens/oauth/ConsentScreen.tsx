import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { oauthApi, type OAuthRequest } from '../../services/api'
import { stepUpDetailOf } from '../../services/stepUp'
import ConfirmItHost from '../../shell/ConfirmItDialog'
import { ToastProvider } from '../../shell/toast'
import { Icon } from '../../shared/icons'
import AuthLayout from '../login/AuthLayout'
import { requestErrorMessage } from '../login/requestErrorMessage'

const GONE = 'This request has expired or was already answered. Start again from the app.'

type State =
  | { kind: 'loading' }
  | { kind: 'gone'; text: string }
  | { kind: 'failed'; text: string }
  | { kind: 'ready'; request: OAuthRequest }
  | { kind: 'leaving'; host: string; approved: boolean }

/**
 * Where /authorize sends the browser (#4789): an MCP client asks to act as the
 * person signed in. The session cookie is SameSite=strict, so the browser
 * arrives without it; ProtectedRoute signs the person in and returns here, and
 * every call is made from this page.
 */
export default function ConsentScreen() {
  const [params] = useSearchParams()
  const requestId = params.get('request') ?? ''
  const { user } = useAuth()
  const [state, setState] = useState<State>({ kind: 'loading' })
  const [busy, setBusy] = useState<'approve' | 'deny' | null>(null)
  const [error, setError] = useState('')

  const load = () => {
    if (!requestId) return setState({ kind: 'gone', text: 'This link names no request. Start again from the app.' })
    setState({ kind: 'loading' })
    oauthApi
      .read(requestId)
      .then((r) => setState({ kind: 'ready', request: r.data }))
      .catch((e) =>
        setState(
          e?.response?.status === 404
            ? { kind: 'gone', text: requestErrorMessage(e, GONE) }
            : { kind: 'failed', text: requestErrorMessage(e, 'Vigil couldn’t read this request.') },
        ),
      )
  }
  useEffect(load, [requestId])

  const decide = async (decision: 'approve' | 'deny', host: string) => {
    setBusy(decision)
    setError('')
    try {
      const r = await oauthApi.decide(requestId, decision)
      setState({ kind: 'leaving', host, approved: decision === 'approve' })
      // a full-page navigation: the app's redirect carries the code, or access_denied
      window.location.assign(r.data.redirect_to)
    } catch (e: any) {
      if (e?.response?.status === 404) setState({ kind: 'gone', text: requestErrorMessage(e, GONE) })
      else if (stepUpDetailOf(e))
        // "Confirm it's you" was cancelled: nothing was decided
        setError('Approving connects the app as you, so Vigil asks you to confirm it’s you first.')
      else setError(requestErrorMessage(e, 'Vigil couldn’t record your answer. Try again.'))
      setBusy(null)
    }
  }

  // Inside the page's root, so the dialog and its toasts take the theme. The decide API asks
  // before an approval; api.ts hands that refusal to ConfirmItHost and retries once confirmed.
  const overlay = (
    <ToastProvider>
      <ConfirmItHost />
    </ToastProvider>
  )
  const role = user?.role_id.replace(/^role-/, '').replace(/-/g, ' ')
  return (() => {
    switch (state.kind) {
      case 'loading':
        return <AuthLayout overlay={overlay} screenLabel="Connect an app" title="Connect an app" subtitle="Reading the app’s request…" />
      case 'gone':
        return <AuthLayout overlay={overlay} screenLabel="Connect an app" title="This request has ended" subtitle="Nothing was connected." message={{ text: state.text }} />
      case 'failed':
        return (
          <AuthLayout overlay={overlay} screenLabel="Connect an app" title="Connect an app" subtitle="The request couldn’t be read." message={{ text: state.text }}>
            <button className="btn-signin" type="button" onClick={load}>
              <Icon name="refresh" /> Try again
            </button>
          </AuthLayout>
        )
      case 'leaving':
        return (
          <AuthLayout
            overlay={overlay}
            screenLabel="Connect an app"
            title={state.approved ? 'Connected' : 'Not connected'}
            subtitle={`Returning you to ${state.host}…`}
          />
        )
      case 'ready': {
        const r = state.request
        const name = r.client_name || 'An unnamed app'
        return (
          <AuthLayout
            overlay={overlay}
            screenLabel="Connect an app"
            title={`Connect ${r.client_name || 'this app'}?`}
            subtitle="It is asking to use Vigil’s MCP tools as you."
            message={error ? { text: error } : undefined}
          >
            <dl className="consent-facts">
              <dt>App</dt>
              <dd>{name}</dd>
              <dt>Returns to</dt>
              <dd className="mono">{r.redirect_host}</dd>
              <dt>Resource</dt>
              <dd className="mono">{r.resource}</dd>
              <dt>Acts as</dt>
              <dd>
                {user?.full_name || user?.username}
                {role && <span className="consent-role">{role} role</span>}
              </dd>
            </dl>
            <p className="consent-note">
              It can do whatever your role lets you do through these tools, recorded against your name, until it
              expires or you end it. It shows as a connected app under Settings › Users › API tokens; revoke it
              there to end it.
            </p>
            <div className="consent-actions">
              <button className="btn-sso" type="button" disabled={!!busy} onClick={() => decide('deny', r.redirect_host)}>
                {busy === 'deny' ? 'Denying…' : 'Deny'}
              </button>
              <button className="btn-signin" type="button" disabled={!!busy} onClick={() => decide('approve', r.redirect_host)}>
                {busy === 'approve' ? 'Approving…' : 'Approve'}
              </button>
            </div>
          </AuthLayout>
        )
      }
    }
  })()
}
