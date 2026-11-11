import { useEffect, useState } from 'react'
import { requestErrorMessage } from '../login/requestErrorMessage'
import { mfaApi } from '../../services/api'
import { Icon } from '../../shared/icons'
import MfaMethodsList, { type MfaMethod } from '../../shared/MfaMethodsList'
import { Popup } from '../../shared/ui'
import { MfaResetDialog } from './RecoveryDialogs'

/** Someone else's sign-in methods, for a holder of users.write over the account, who may reset them. */
export default function UserMethodsDialog({
  user,
  notify,
  canReset,
  onClose,
  onReset,
  onResetPassword,
}: {
  user: { user_id: string; username: string }
  notify: (kind: 'ok' | 'err', msg: string) => void
  canReset: boolean
  onClose: () => void
  onReset?: () => void
  onResetPassword?: () => void
}) {
  const [rows, setRows] = useState<MfaMethod[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [resetting, setResetting] = useState(false)

  useEffect(() => {
    mfaApi
      .ofUser(user.user_id)
      .then((res) => setRows(res.data))
      .catch((err) => setError(requestErrorMessage(err, "Couldn't load their sign-in methods.")))
  }, [user.user_id])

  if (resetting) {
    return <MfaResetDialog user={user} notify={notify} onClose={onClose} onDone={onReset} />
  }

  return (
    <Popup open onClose={onClose} title={`Sign-in methods · ${user.username}`} width={520}>
      <div className="flex flex-col gap-3.5">
        {error && (
          <div className="settings-banner err" role="alert">
            <Icon name="alert" size={14} /> {error}
          </div>
        )}
        {rows === null && !error ? <div className="text-sm text-tx-3 py-6 text-center">Loading…</div> : rows && <MfaMethodsList methods={rows} />}
        <div className="flex justify-end gap-2.5 flex-wrap">
          {canReset && onResetPassword && (
            <button className="btn ghost" onClick={onResetPassword}>
              <Icon name="key" /> Reset password
            </button>
          )}
          {canReset && !!rows?.length && (
            <button className="btn danger" onClick={() => setResetting(true)}>
              Reset two-step sign-in
            </button>
          )}
          <button className="btn ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </Popup>
  )
}
