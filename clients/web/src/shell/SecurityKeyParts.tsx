import { Icon } from '../shared/icons'
import { SECURITY_KEYS_DOCS } from '../services/webauthn'

/** Strong checks never wait in the dialog: the prompt comes after Continue. */
export const KEY_AFTER_CONTINUE =
  "After you continue, touch your security key, or follow your browser's prompt to use a passkey."

/** The "Sign-in methods" line shown in place of key controls when this install has no security keys. */
export function KeysUnavailable({ reason }: { reason?: string | null }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="text-xs text-tx-3">Sign-in methods</div>
      <p className="m-0 text-sm text-tx-2 leading-relaxed">
        {reason}{' '}
        <a href={SECURITY_KEYS_DOCS} target="_blank" rel="noreferrer" className="data-link" style={{ padding: 0, fontWeight: 600 }}>
          About security keys
        </a>
      </p>
    </div>
  )
}

/** The one-factor key wait: a breathing key icon and what to do. Links are the caller's. */
export function KeyWaiting({ waiting, children }: { waiting: boolean; children: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2.5 px-4 py-5 text-center rounded-sm bg-bg-2 border border-line">
      <Icon name="key" size={44} className={`text-accent-2 ${waiting ? 'animate-pulse' : ''}`} />
      <p className="m-0 text-sm text-tx leading-relaxed">
        Touch your security key, or follow your browser&apos;s prompt to use a passkey.
      </p>
      <p className="m-0 text-xs text-tx-3" aria-live="polite">
        {waiting ? 'Waiting for your key…' : 'No key was used.'}
      </p>
      <div className="flex flex-wrap justify-center gap-x-4 gap-y-1">{children}</div>
    </div>
  )
}
