import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { knownActivityApi, type KnownActivity, type KnownActivityList } from '../../services/api'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { kindLabel, KnownActivityForm } from '../../shared/KnownActivityForm'
import '../../shared/knownActivity.css'
import { PageHead } from '../../shared/PageHead'
import { ConfirmDialog } from '../../shared/ui'
import { utcDay } from '../../shared/utc'
import type { SectionProps } from './types'

export const KNOWN_ACTIVITY_DESC =
  'Record that an entity’s activity is expected, why, and until when. Known activity only explains: people and agents see it beside an alert, and the alert stays as it is. IP exclusions, on the Findings page, hide alerts.'

const ORIGIN_LABEL: Record<string, string> = { ad_hoc: 'Settings', finding: 'Alert', case: 'Case', run: 'Run' }
const STATE_LABEL: Record<KnownActivity['state'], string> = { active: 'Active', expired: 'Expired', removed: 'Removed' }

export default function KnownActivitySection({ notify }: SectionProps) {
  const canWrite = useAuth().hasPermission('findings.write')
  const [list, setList] = useState<KnownActivityList | null>(null)
  const [loadError, setLoadError] = useState('')
  const [showEnded, setShowEnded] = useState(false)
  const [adding, setAdding] = useState(false)
  const [removing, setRemoving] = useState<KnownActivity | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    setLoadError('')
    return knownActivityApi
      .list(showEnded)
      .then((res) => setList(res.data))
      .catch((e) => setLoadError(errorText(e, 'Failed to load known activity')))
  }, [showEnded])

  useEffect(() => {
    void load()
  }, [load])

  const remove = async () => {
    if (!removing) return
    setBusy(true)
    try {
      await knownActivityApi.remove(removing.known_activity_id)
      notify('ok', `Removed known activity on ${removing.entity_value}.`)
      setRemoving(null)
      await load()
    } catch (e) {
      notify('err', errorText(e, 'Failed to remove known activity'))
    } finally {
      setBusy(false)
    }
  }

  const rows = list?.known_activity ?? []

  return (
    <>
      <PageHead
        title="Known activity"
        description={KNOWN_ACTIVITY_DESC}
        actions={
          <>
            <label className="flex items-center gap-2 text-sm text-tx-2">
              <input type="checkbox" checked={showEnded} onChange={(e) => setShowEnded(e.target.checked)} />
              Show expired and removed
            </label>
            {canWrite && (
              <button className="btn primary" disabled={!list} onClick={() => setAdding(true)}><Icon name="plus" /> Record known activity</button>
            )}
          </>
        }
      />

      {adding && list && (
        <section className="card card-sq settings-card wide" aria-label="New known activity">
          <div className="card-b">
            <KnownActivityForm
              kinds={list.entity_kinds}
              defaultExpiresAt={list.default_expires_at}
              onCancel={() => setAdding(false)}
              onSaved={(rule) => {
                notify('ok', `Recorded known activity on ${rule.entity_value}.`)
                setAdding(false)
                void load()
              }}
            />
          </div>
        </section>
      )}

      <section className="card card-sq settings-card wide" aria-label="Known activity rules">
        <div className="card-b table-wrap">
          <table className="tbl ka-tbl">
            <thead>
              <tr>
                <th>Entity</th>
                <th>Why it is expected</th>
                <th>Recorded by</th>
                <th>Until</th>
                <th>From</th>
                <th>State</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {loadError && (
                <tr><td colSpan={7} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>Couldn’t load known activity: {loadError}</td></tr>
              )}
              {!loadError && list && rows.length === 0 && (
                <tr><td colSpan={7} className="muted" style={{ textAlign: 'center', padding: '28px 0' }}>Nothing is recorded as known activity.</td></tr>
              )}
              {rows.map((rule) => (
                <tr key={rule.known_activity_id} className={rule.state === 'active' ? '' : 'ka-ended'}>
                  <td>
                    <div className="mono">{rule.entity_value}</div>
                    <div className="muted">{kindLabel(rule.entity_kind)}</div>
                  </td>
                  <td className="ka-reason">
                    {rule.reason}
                    {rule.removed_at && (
                      <div className="muted">
                        Removed {utcDay(rule.removed_at)} by {rule.removed_by}
                        {rule.removal_reason ? `: ${rule.removal_reason}` : ''}
                      </div>
                    )}
                  </td>
                  <td>
                    {rule.created_by}
                    <div className="muted">{utcDay(rule.created_at)}</div>
                  </td>
                  <td>{utcDay(rule.expires_at)}</td>
                  <td>
                    {ORIGIN_LABEL[rule.origin] ?? rule.origin}
                    {rule.origin_ref && <div className="mono muted">{rule.origin_ref}</div>}
                  </td>
                  <td>{STATE_LABEL[rule.state]}</td>
                  <td style={{ textAlign: 'right' }}>
                    {canWrite && rule.state === 'active' && (
                      <button className="btn ghost" aria-label={`Remove known activity on ${rule.entity_value}`} onClick={() => setRemoving(rule)}>Remove</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <ConfirmDialog
        open={removing !== null}
        title={`Remove known activity on ${removing?.entity_value ?? ''}?`}
        body="Alerts that name it stay as they are. People and agents stop seeing this explanation. The record stays, marked removed, and the removal goes on the audit trail."
        confirmLabel="Remove"
        busy={busy}
        onConfirm={remove}
        onClose={() => setRemoving(null)}
      />
    </>
  )
}
