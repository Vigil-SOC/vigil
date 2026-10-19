import { useEffect, useState, type FormEvent } from 'react'
import QRCode from 'qrcode'
import { useAuth } from '../contexts/AuthContext'
import { mfaApi } from '../services/api'
import { Icon } from '../shared/icons'
import { ConfirmDialog, Field, Popup, TextInput } from '../shared/ui'

type View = 'main' | 'enrol' | 'codes' | 'disable'

function errText(e: unknown, fallback: string): string {
  const err = e as { response?: { status?: number; data?: { detail?: unknown } }; message?: string }
  if (err.response?.status === 429) return 'Too many attempts. Wait a minute and try again.'
  const detail = err.response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

function RecoveryCodes({ codes }: { codes: string[] }) {
  const [copied, setCopied] = useState(false)

  const copy = () => {
    void navigator.clipboard?.writeText(codes.join('\n')).then(() => setCopied(true))
  }
  const download = () => {
    const url = URL.createObjectURL(new Blob([codes.join('\n') + '\n'], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'vigil-recovery-codes.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="settings-banner info">
        <Icon name="alert" size={14} />
        Save these now. Each code works once, and they cannot be shown again.
      </div>
      <ul
        className="grid grid-cols-2 gap-2 m-0 p-3 list-none bg-bg-2 border border-line rounded-sm font-mono text-[13px] text-tx"
        aria-label="Your recovery codes"
      >
        {codes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <div className="flex gap-2.5">
        <button className="btn ghost" onClick={copy}>
          <Icon name="copy" size={14} /> {copied ? 'Copied' : 'Copy'}
        </button>
        <button className="btn ghost" onClick={download}>
          <Icon name="download" size={14} /> Download
        </button>
      </div>
    </div>
  )
}

export default function TwoStepDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user, refreshUser } = useAuth()
  const enabled = !!user?.mfa_enabled
  const [view, setView] = useState<View>('main')
  const [secret, setSecret] = useState('')
  const [qr, setQr] = useState('')
  const [codes, setCodes] = useState<string[]>([])
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmRegen, setConfirmRegen] = useState(false)

  useEffect(() => {
    if (!open) {
      setView('main')
      setSecret('')
      setQr('')
      setCodes([])
      setCode('')
      setError(null)
      setConfirmRegen(false)
    }
  }, [open])

  const go = (next: View) => {
    setView(next)
    setCode('')
    setError(null)
  }

  // Runs one API call with shared busy/error handling; returns undefined on failure.
  const run = async <T,>(call: () => Promise<T>, fallback: string): Promise<T | undefined> => {
    setBusy(true)
    setError(null)
    try {
      return await call()
    } catch (e) {
      setError(errText(e, fallback))
      return undefined
    } finally {
      setBusy(false)
    }
  }

  const startSetup = async () => {
    const res = await run(() => mfaApi.setup(), 'Could not start setup.')
    if (!res) return
    // The otpauth URI carries the secret: render it here, never via an external QR service.
    const dataUrl = await QRCode.toDataURL(res.data.qr_uri, { margin: 2, width: 176 }).catch(() => '')
    setSecret(res.data.secret)
    setQr(dataUrl)
    go('enrol')
  }

  const verify = async (e: FormEvent) => {
    e.preventDefault()
    const res = await run(() => mfaApi.verify(code.trim()), 'Invalid MFA code')
    if (!res) return
    setCodes(res.data.recovery_codes)
    go('codes')
    await refreshUser()
  }

  const regenerate = async () => {
    setConfirmRegen(false)
    const res = await run(() => mfaApi.regenerateRecoveryCodes(), 'Could not regenerate recovery codes.')
    if (!res) return
    setCodes(res.data.recovery_codes)
    go('codes')
  }

  const disable = async (e: FormEvent) => {
    e.preventDefault()
    const res = await run(() => mfaApi.disable(code.trim()), 'Invalid MFA code')
    if (!res) return
    await refreshUser()
    go('main')
  }

  const banner = error && (
    <div className="settings-banner err" role="alert">
      <Icon name="alert" size={14} /> {error}
    </div>
  )

  const title =
    view === 'enrol'
      ? 'Set up two-step sign-in'
      : view === 'codes'
        ? 'Recovery codes'
        : view === 'disable'
          ? 'Turn off two-step sign-in'
          : 'Two-step sign-in'

  return (
    <>
      <Popup open={open} onClose={onClose} title={title} width={460}>
        <div className="flex flex-col gap-3.5">
          {banner}

          {view === 'main' && !enabled && (
            <>
              <p className="m-0 text-sm text-tx-2 leading-relaxed">
                Add a second step to signing in: a 6-digit code from an authenticator app, in addition to
                your password.
              </p>
              <div className="flex justify-end">
                <button className="btn primary" onClick={startSetup} disabled={busy}>
                  {busy ? 'Working…' : 'Set up'}
                </button>
              </div>
            </>
          )}

          {view === 'main' && enabled && (
            <>
              <div className="settings-banner ok">
                <Icon name="shield" size={14} /> Two-step sign-in is on.
              </div>
              <p className="m-0 text-sm text-tx-2 leading-relaxed">
                If you lose your authenticator, a recovery code lets you sign in once. Regenerating makes
                the old codes stop working.
              </p>
              <div className="flex justify-end gap-2.5">
                <button className="btn ghost" onClick={() => setConfirmRegen(true)} disabled={busy}>
                  Regenerate recovery codes
                </button>
                <button className="btn danger" onClick={() => go('disable')} disabled={busy}>
                  Turn off
                </button>
              </div>
            </>
          )}

          {view === 'enrol' && (
            <form className="flex flex-col gap-3.5" onSubmit={verify}>
              <p className="m-0 text-sm text-tx-2 leading-relaxed">
                Scan this with an authenticator app, or type the key in by hand. Then enter the 6-digit code
                it shows.
              </p>
              <div className="flex flex-col items-center gap-2.5">
                {qr && (
                  <img
                    src={qr}
                    width={176}
                    height={176}
                    alt="QR code for your authenticator app"
                    className="rounded-sm bg-white"
                  />
                )}
                <code
                  className="font-mono text-[13px] text-tx tracking-wider break-all text-center select-all"
                  aria-label="Setup key"
                >
                  {secret}
                </code>
              </div>
              <Field label="Authentication code">
                <TextInput
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  autoFocus
                />
              </Field>
              <div className="flex justify-end gap-2.5">
                <button type="button" className="btn ghost" onClick={onClose} disabled={busy}>
                  Cancel
                </button>
                <button type="submit" className="btn primary" disabled={busy || code.trim().length < 6}>
                  {busy ? 'Verifying…' : 'Turn on'}
                </button>
              </div>
            </form>
          )}

          {view === 'codes' && (
            <>
              <RecoveryCodes codes={codes} />
              <div className="flex justify-end">
                <button className="btn primary" onClick={onClose}>
                  Done
                </button>
              </div>
            </>
          )}

          {view === 'disable' && (
            <form className="flex flex-col gap-3.5" onSubmit={disable}>
              <Field
                label="Authentication or recovery code"
                hint="Enter a current code from your authenticator app, or an unused recovery code."
              >
                <TextInput
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  autoComplete="one-time-code"
                  autoFocus
                />
              </Field>
              <div className="flex justify-end gap-2.5">
                <button type="button" className="btn ghost" onClick={() => go('main')} disabled={busy}>
                  Back
                </button>
                <button type="submit" className="btn danger" disabled={busy || !code.trim()}>
                  {busy ? 'Working…' : 'Turn off'}
                </button>
              </div>
            </form>
          )}
        </div>
      </Popup>

      <ConfirmDialog
        open={open && confirmRegen}
        title="Regenerate recovery codes?"
        body="Your current recovery codes will stop working. New codes are shown once."
        confirmLabel="Regenerate"
        danger={false}
        busy={busy}
        onConfirm={regenerate}
        onClose={() => setConfirmRegen(false)}
      />
    </>
  )
}
