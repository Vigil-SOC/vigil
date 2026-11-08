import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { autonomyApi, type RolloutLimits } from '../../services/api'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { ScrubField } from '../../shared/ScrubField'
import { ConfirmDialog, Field, SettingsCard, TextInput } from '../../shared/ui'
import type { SectionProps } from './types'

type LimitField = 'live_per_day' | 'shadow_days' | 'max_hits_per_day'

interface Row {
  field: LimitField
  title: string
  hint: string
  label: string
  unit: string
  /** Which way is looser: more promotions or hits, or fewer days in shadow. */
  looser: 'up' | 'down'
  ask: (next: number, now: number) => { title: string; body: string }
}

const plural = (n: number, one: string, many: string) => `${n.toLocaleString()} ${n === 1 ? one : many}`

const ROWS: Row[] = [
  {
    field: 'live_per_day', title: 'Rules promoted per day', hint: 'Most of Vigil’s own rules that may go live in one day',
    label: 'Promotions', unit: 'a day', looser: 'up',
    ask: (next, now) => ({ title: `Promote up to ${plural(next, 'rule', 'rules')} a day?`, body: `More of Vigil’s rules reach analysts each day (now ${now}).` }),
  },
  {
    field: 'shadow_days', title: 'Days in shadow', hint: 'A rule runs alerting nobody this long before it can go live',
    label: 'Shadow', unit: 'days', looser: 'down',
    ask: (next, now) => ({ title: `Let rules go live after ${plural(next, 'day', 'days')} in shadow?`, body: `Less time to see a rule misfire before analysts get its alerts (now ${now}).` }),
  },
  {
    field: 'max_hits_per_day', title: 'Most hits a day', hint: 'Above this on any day, a rule stays in shadow, or goes back from live',
    label: 'Hits', unit: 'a day', looser: 'up',
    ask: (next, now) => ({ title: `Allow up to ${plural(next, 'hit', 'hits')} a day?`, body: `Louder rules can go live and stay live (now ${now.toLocaleString()}).` }),
  },
]

/** Settings › Limits & autonomy: how fast Vigil moves its own rules from shadow to live
 * (GET/PUT /api/autonomy/rollout-limits). Tightening saves at once; loosening asks first,
 * with an optional reason for the audit trail. Quiet, Noisy and Silent stay on each SIEM. */
export default function RolloutLimitsCard({ notify, canWrite }: { notify: SectionProps['notify']; canWrite: boolean }) {
  const [data, setData] = useState<RolloutLimits | null>(null)
  const [failed, setFailed] = useState(false)
  const [pending, setPending] = useState<{ row: Row; value: number } | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    autonomyApi.rolloutLimits()
      .then((res) => { if (live) setData(res.data) })
      .catch(() => { if (live) setFailed(true) })
    return () => { live = false }
  }, [])

  const close = () => { setPending(null); setReason('') }
  const save = (field: LimitField, value: number, why = '') => {
    setBusy(true)
    autonomyApi.updateRolloutLimits({ [field]: value, ...(why.trim() ? { reason: why.trim() } : {}) })
      .then((res) => { setData(res.data); notify('ok', 'Saved. The next rule sweep uses it.') })
      .catch((e) => notify('err', errorText(e, 'The limit was not saved.')))
      .finally(() => { setBusy(false); close() })
  }
  const commit = (row: Row, value: number) => {
    const now = data?.limits[row.field]
    if (now === undefined) return
    const looser = row.looser === 'up' ? value > now : value < now
    if (looser) setPending({ row, value })
    else save(row.field, value)
  }

  const ask = pending && data ? pending.row.ask(pending.value, data.limits[pending.row.field]) : null
  return (
    <SettingsCard
      wide
      title="Detection rules"
      desc="How fast Vigil moves its own rules from shadow to live. The next rule sweep uses these. Click a value to type a new one. Tightening applies at once; loosening asks you to confirm."
    >
      {failed && (
        <div className="settings-banner err">
          <Icon name="alert" size={14} />
          <span>Could not load the rule rollout limits. Reload to try again.</span>
        </div>
      )}
      {data && ROWS.map((r) => {
        const value = data.limits[r.field]
        const d = data.defaults[r.field]
        const b = data.bounds[r.field]
        return (
          <div className="lim-row" key={r.field}>
            <span className="lim-row-title">
              {r.title}
              <span>{r.hint}</span>
            </span>
            <span className="lim-row-tag">{value === d ? 'Default' : `Changed · default ${d.toLocaleString()}`}</span>
            <span className="lim-row-ctl">
              <ScrubField
                name={r.title} label={r.label} unit={r.unit} value={value} min={b?.min ?? value} max={b?.max ?? value} step={1}
                readOnly={!canWrite || !b} onCommit={(v) => commit(r, v)}
              />
            </span>
          </div>
        )
      })}
      {data && (
        <p className="lim-foot">
          Quiet, Noisy and Silent thresholds are set on each SIEM:{' '}
          <Link to="/settings?section=integrations">Elastic and Wazuh</Link> · <Link to="/settings?section=integrations">Splunk</Link>
        </p>
      )}
      <ConfirmDialog
        open={!!pending}
        title={ask?.title ?? ''}
        body={
          <>
            <p>{ask?.body}</p>
            <Field label="Reason (optional)" hint="Kept on the audit trail with your name.">
              <TextInput value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
            </Field>
          </>
        }
        confirmLabel="Loosen"
        busy={busy}
        onConfirm={() => pending && save(pending.row.field, pending.value, reason)}
        onClose={close}
      />
    </SettingsCard>
  )
}
