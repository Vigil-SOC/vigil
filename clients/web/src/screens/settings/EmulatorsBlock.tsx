import { useState } from 'react'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, TextInput } from '../../shared/ui'
import type { SectionProps } from './types'
import type { useEmulation } from './useSettings'

// As core/response/emulation.py: one hostname, nothing a runner could read as a flag
const validHost = (h: string) => h.length > 0 && h.length <= 253 && !h.startsWith('-') && !/\s/.test(h)
const STOPPED_LOOSEN = 'Stopped: resume first'

/** Settings › Limits & autonomy: the hosts emulators may run on. A tool is an emulator by its binding, set on
 * its classify row. Adding a host needs ai_autonomy.write and asks first; removing needs ai_autonomy.stop. */
export default function EmulatorsBlock({
  emulation,
  stopped,
  canRaise,
  canLower,
  notify,
}: {
  emulation: ReturnType<typeof useEmulation>
  stopped: boolean
  canRaise: boolean
  canLower: boolean
  notify: SectionProps['notify']
}) {
  const { data, failed, save } = emulation
  const [host, setHost] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const run = (body: { hosts: string[] }, ok: string) => {
    setBusy(true)
    save(body)
      .then(() => notify('ok', ok))
      .catch((err) => notify('err', errorText(err, 'The emulators were not saved.')))
      .finally(() => {
        setBusy(false)
        setPending(null)
      })
  }
  const confirm = () => {
    if (!data || !pending) return
    run({ hosts: [...data.hosts, pending] }, 'Host listed. Emulators may run on it now.')
    setHost('')
  }
  const typed = host.trim()
  const known = data?.hosts.some((h) => h.toLowerCase() === typed.toLowerCase())
  const hostError =
    typed && !validHost(typed) ? 'One hostname, with no spaces and no leading “-”.' : known ? 'Already listed.' : ''
  const addHost = () => typed && !hostError && setPending(typed)
  const mayAdd = canRaise && !stopped

  return (
    <div className="lim-tools mt-3">
      <div className="lim-tools-head">
        <h4>Emulators</h4>
        <span>
          Emulators run only on the hosts listed here; any other host is refused before the call leaves, and an empty
          list refuses every emulation. Some techniques change a host in ways that cannot be undone. A tool becomes an
          emulator when its row under Outside tools is bound to Emulator.
        </span>
        {!canLower && <span>Your role can see these but not change them.</span>}
        {canLower && !canRaise && <span>Your role can remove a host. Adding one needs permission to loosen autonomy.</span>}
        {stopped && canRaise && (
          <span className="lim-class-note">
            <Icon name="alert" size={13} /> {STOPPED_LOOSEN}
          </span>
        )}
      </div>
      {failed && !data && <span className="text-xs text-tx-3">Could not load the emulators. Reload to try again.</span>}
      {data && (
        <>
          <div className="lim-row lim-emu-hosts lim-emu-wide">
            <span className="lim-row-title">
              Hosts emulators may run on
              <span>A hostname matches a listed one exactly, ignoring case. Vigil sends the list with each call.</span>
            </span>
            <span className={`lim-row-tag${data.hosts.length ? '' : ' fair'}`}>
              {data.hosts.length
                ? `${data.hosts.length} ${data.hosts.length === 1 ? 'host' : 'hosts'}`
                : 'None listed: every emulation is refused'}
            </span>
            <span className="lim-row-ctl lim-emu-add">
              {canRaise && (
                <>
                  <TextInput
                    aria-label="Host to list"
                    placeholder="lab-win10-01"
                    value={host}
                    disabled={!mayAdd || busy}
                    title={stopped ? STOPPED_LOOSEN : undefined}
                    onChange={(e) => setHost(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && addHost()}
                  />
                  <button
                    type="button"
                    className="btn"
                    disabled={!mayAdd || busy || !typed || !!hostError}
                    onClick={addHost}
                  >
                    Add
                  </button>
                </>
              )}
            </span>
            {hostError && <span className="lim-emu-err">{hostError}</span>}
            {data.hosts.length > 0 && (
              <span className="lim-emu-chips">
                {data.hosts.map((h) => (
                  <span className="chip" key={h}>
                    <span className="lim-tool-name">{h}</span>
                    {canLower && (
                      <button
                        type="button"
                        className="lim-emu-x"
                        aria-label={`Remove ${h}`}
                        disabled={busy}
                        onClick={() => run({ hosts: data.hosts.filter((x) => x !== h) }, `${h} removed.`)}
                      >
                        <Icon name="close" size={11} />
                      </button>
                    )}
                  </span>
                ))}
              </span>
            )}
          </div>
        </>
      )}
      <ConfirmDialog
        open={pending != null}
        title={`Let emulators run on ${pending ?? ''}?`}
        body="Every emulator may then run techniques on this host, and some change a host in ways that cannot be undone. Your name goes on the audit trail."
        confirmLabel="List host"
        busy={busy}
        onConfirm={confirm}
        onClose={() => !busy && setPending(null)}
      />
    </div>
  )
}
