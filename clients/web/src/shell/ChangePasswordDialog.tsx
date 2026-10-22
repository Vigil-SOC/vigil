import { useEffect, useRef, useState, type FormEvent } from 'react'
import { requestErrorMessage } from '../screens/login/requestErrorMessage'
import { authApi } from '../services/api'
import { Icon } from '../shared/icons'
import { Field, PasswordInput, Popup } from '../shared/ui'

interface PasswordForm {
  current: string
  next: string
  confirm: string
}

type PasswordProblems = Partial<Record<keyof PasswordForm, string>>

/**
 * Only what the browser can know on its own. Length, strength and history
 * reuse are the server's policy (AUTH_MIN_PASSWORD_LENGTH and friends), and
 * its refusal comes back with the exact rule, so the client does not keep a
 * copy of those numbers that could drift from a deployment's settings.
 */
function passwordProblems({ current, next, confirm }: PasswordForm): PasswordProblems {
  const problems: PasswordProblems = {}
  if (!current) problems.current = 'Enter your current password.'
  if (!next) problems.next = 'Enter a new password.'
  else if (next === current) problems.next = 'Choose a password different from your current one.'
  if (confirm !== next) problems.confirm = 'This does not match the new password.'
  return problems
}

/**
 * Current, new and confirm. A successful change signs the user out everywhere
 * (the server revokes every token and clears the cookies), so the dialog then
 * says so and its only way out is `onSignedOut`.
 */
export default function ChangePasswordDialog({
  open,
  onClose,
  onSignedOut,
}: {
  open: boolean
  onClose: () => void
  /** called once the change has gone through and the user has read that they are signed out */
  onSignedOut: () => void
}) {
  const [form, setForm] = useState<PasswordForm>({ current: '', next: '', confirm: '' })
  const [touched, setTouched] = useState<Record<keyof PasswordForm, boolean>>({
    current: false,
    next: false,
    confirm: false,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  // PasswordInput does not forward refs, so the first field is found through the form
  const formRef = useRef<HTMLFormElement>(null)
  const doneRef = useRef<HTMLButtonElement>(null)

  // Reset whenever the dialog opens, so a cancelled attempt leaves nothing behind.
  useEffect(() => {
    if (!open) return
    setForm({ current: '', next: '', confirm: '' })
    setTouched({ current: false, next: false, confirm: false })
    setBusy(false)
    setError(null)
    setDone(false)
  }, [open])

  // Popup focuses its panel after its children mount, so wait a tick before
  // moving focus to the first field or to the sign-in button.
  useEffect(() => {
    if (!open) return
    const id = setTimeout(() => (done ? doneRef.current : formRef.current?.querySelector('input'))?.focus(), 0)
    return () => clearTimeout(id)
  }, [open, done])

  const problems = passwordProblems(form)
  const valid = Object.keys(problems).length === 0
  // the confirm check speaks up once it is as long as the new password, without waiting for blur
  const showConfirm = touched.confirm || (form.confirm.length > 0 && form.confirm.length >= form.next.length)
  const shown: PasswordProblems = {
    current: touched.current ? problems.current : undefined,
    next: touched.next && form.next ? problems.next : undefined,
    confirm: showConfirm && form.confirm ? problems.confirm : undefined,
  }

  const set = (key: keyof PasswordForm) => (value: string) => {
    setForm((f) => ({ ...f, [key]: value }))
    setError(null)
  }
  const touch = (key: keyof PasswordForm) => () => setTouched((t) => ({ ...t, [key]: true }))

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!valid || busy) return
    setBusy(true)
    setError(null)
    try {
      await authApi.changePassword({ current_password: form.current, new_password: form.next })
      setForm({ current: '', next: '', confirm: '' })
      setDone(true)
    } catch (err) {
      setError(requestErrorMessage(err, "Couldn't change your password. Try again."))
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    // The old session is already gone server-side; closing any way signs out.
    return (
      <Popup open={open} onClose={onSignedOut} title="Password changed" width={440}>
        <div className="flex flex-col gap-3.5">
          <div className="settings-banner ok" role="status">
            <Icon name="check2" size={14} /> Your password has been changed.
          </div>
          <p className="text-sm text-tx-2 leading-relaxed" style={{ margin: 0 }}>
            You have been signed out on every device. Sign in again with your new password.
          </p>
          <div className="flex justify-end gap-2.5 mt-1">
            <button ref={doneRef} type="button" className="btn primary" onClick={onSignedOut}>
              Sign in again
            </button>
          </div>
        </div>
      </Popup>
    )
  }

  return (
    // Not closable mid-request: a change that lands after the dialog is gone
    // would sign the user out with nothing on screen to say why.
    <Popup open={open} onClose={() => !busy && onClose()} title="Change password" width={440}>
      <form ref={formRef} className="flex flex-col gap-3.5" onSubmit={submit} noValidate>
        <p className="text-sm text-tx-2 leading-relaxed" style={{ margin: 0 }}>
          When your password changes you will be signed out on every device, and you will
          sign in again with the new one.
        </p>
        {error && (
          <div className="settings-banner err" role="alert">
            <Icon name="alert" size={14} /> {error}
          </div>
        )}
        <Field label="Current password" error={shown.current}>
          <PasswordInput
            name="current-password"
            autoComplete="current-password"
            value={form.current}
            disabled={busy}
            aria-invalid={shown.current ? true : undefined}
            onChange={(e) => set('current')(e.target.value)}
            onBlur={touch('current')}
          />
        </Field>
        <Field
          label="New password"
          hint="At least 12 characters. A few unrelated words make a strong one."
          error={shown.next}
        >
          <PasswordInput
            name="new-password"
            autoComplete="new-password"
            value={form.next}
            disabled={busy}
            aria-invalid={shown.next ? true : undefined}
            onChange={(e) => set('next')(e.target.value)}
            onBlur={touch('next')}
          />
        </Field>
        <Field label="Confirm new password" error={shown.confirm}>
          <PasswordInput
            name="confirm-password"
            autoComplete="new-password"
            value={form.confirm}
            disabled={busy}
            aria-invalid={shown.confirm ? true : undefined}
            onChange={(e) => set('confirm')(e.target.value)}
            onBlur={touch('confirm')}
          />
        </Field>
        <div className="flex justify-end gap-2.5 mt-1">
          <button type="button" className="btn ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={!valid || busy}>
            {busy ? 'Changing…' : 'Change password'}
          </button>
        </div>
      </form>
    </Popup>
  )
}
