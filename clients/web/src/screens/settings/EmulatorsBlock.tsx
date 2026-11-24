import { useEffect, useState } from 'react'
import { autonomyApi, type ToolClass } from '../../services/api'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, Select, TextInput } from '../../shared/ui'
import type { SectionProps } from './types'
import type { useEmulation } from './useSettings'

// As core/response/emulation.py: one hostname, nothing a runner could read as a flag
const validHost = (h: string) => h.length > 0 && h.length <= 253 && !h.startsWith('-') && !/\s/.test(h)
const STOPPED_LOOSEN = 'Stopped: resume first'
const CLASS_LABEL: Record<NonNullable<ToolClass['effect_class']>, string> = {
  read: 'Read',
  read_unverified: 'Read, unconfirmed',
  reversible: 'Reversible',
  irreversible: 'Cannot be undone',
}

type Pending = { kind: 'tool'; name: string } | { kind: 'host'; host: string }

/** Settings › Limits & autonomy: which outside tools are emulators, and the hosts they may run on.
 * Adding either needs ai_autonomy.write and asks first; removing needs ai_autonomy.stop and applies at once. */
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
  const [tools, setTools] = useState<ToolClass[]>([])
  const [host, setHost] = useState('')
  const [pick, setPick] = useState('')
  const [pending, setPending] = useState<Pending | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    autonomyApi
      .tools()
      .then((r) => setTools(r.data))
      .catch(() => {}) // the marked names still show, without their class
  }, [])

  const run = (body: { tools?: string[]; hosts?: string[] }, ok: string) => {
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
    if (pending.kind === 'tool') {
      run({ tools: [...data.tools, pending.name] }, 'Marked as an emulator.')
      setPick('')
    } else {
      run({ hosts: [...data.hosts, pending.host] }, 'Host listed. Emulators may run on it now.')
      setHost('')
    }
  }
  const typed = host.trim()
  const known = data?.hosts.some((h) => h.toLowerCase() === typed.toLowerCase())
  const hostError =
    typed && !validHost(typed) ? 'One hostname, with no spaces and no leading “-”.' : known ? 'Already listed.' : ''
  const addHost = () => typed && !hostError && setPending({ kind: 'host', host: typed })
  const classOf = (name: string) => tools.find((t) => t.name === name)?.effect_class
  const unmarked = tools.filter((t) => !data?.tools.includes(t.name))
  const mayAdd = canRaise && !stopped

  return (
    <div className="lim-tools mt-3">
      <div className="lim-tools-head">
        <h4>Emulators {data && <span className="lim-tools-count">{data.tools.length}</span>}</h4>
        <span>
          Emulators run only on the hosts listed here; any other host is refused before the call leaves, and an empty
          list refuses every emulation. Some techniques change a host in ways that cannot be undone.
        </span>
        {!canLower && <span>Your role can see these but not change them.</span>}
        {canLower && !canRaise && (
          <span>
            Your role can unmark an emulator or remove a host. Adding either needs permission to loosen autonomy.
          </span>
        )}
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
          {data.tools.map((name) => {
            const cls = classOf(name)
            return (
              <div className="lim-row" key={name}>
                {/* the whole name: an engine id has no server, so splitting it would invent one */}
                <span className="lim-row-title">
                  <span className="lim-tool-name">{name}</span>
                </span>
                <span className="lim-row-tag">
                  <span className="lim-emu-mark">Emulator</span> {cls ? CLASS_LABEL[cls] : 'Not connected'}
                </span>
                <span className="lim-row-ctl">
                  {canLower && (
                    <button
                      type="button"
                      className="btn ghost"
                      disabled={busy}
                      onClick={() =>
                        run({ tools: data.tools.filter((t) => t !== name) }, `${name} is no longer an emulator.`)
                      }
                    >
                      Unmark
                    </button>
                  )}
                </span>
              </div>
            )
          })}
          {canRaise && unmarked.length > 0 && (
            <div className="lim-row lim-emu-wide">
              <span className="lim-row-title">
                Mark a tool as an emulator
                <span>Its calls are held to the host list, and chat never offers it.</span>
              </span>
              <span />
              <span className="lim-row-ctl lim-emu-add">
                <Select
                  label="Outside tool"
                  value={pick}
                  searchable
                  placeholder="Choose a tool…"
                  disabled={!mayAdd || busy}
                  options={unmarked.map((t) => ({
                    value: t.name,
                    label: t.name,
                  }))}
                  onSelect={setPick}
                />
                <button
                  type="button"
                  className="btn"
                  disabled={!mayAdd || busy || !pick}
                  title={stopped ? STOPPED_LOOSEN : undefined}
                  onClick={() => setPending({ kind: 'tool', name: pick })}
                >
                  Mark
                </button>
              </span>
            </div>
          )}
        </>
      )}
      <ConfirmDialog
        open={pending != null}
        title={
          pending?.kind === 'tool'
            ? `Mark ${pending.name} as an emulator?`
            : `Let emulators run on ${pending?.host ?? ''}?`
        }
        body={
          pending?.kind === 'tool'
            ? 'Vigil will refuse its calls on any host not listed here, send it the list, and keep it out of chat. Your name goes on the audit trail.'
            : 'Every emulator may then run techniques on this host, and some change a host in ways that cannot be undone. Your name goes on the audit trail.'
        }
        confirmLabel={pending?.kind === 'tool' ? 'Mark' : 'List host'}
        busy={busy}
        onConfirm={confirm}
        onClose={() => !busy && setPending(null)}
      />
    </div>
  )
}
