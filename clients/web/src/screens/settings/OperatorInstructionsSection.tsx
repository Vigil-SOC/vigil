import { useEffect, useState } from 'react'
import { configApi } from '../../services/api'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import type { SectionProps } from './types'

export const OPERATOR_INSTRUCTIONS_DESC =
  'House rules every agent follows on this deployment: the language it writes in, how it states times, business hours, and what to escalate.'

const PLACEHOLDER = [
  'Write case titles, conclusions and proposed actions in Japanese.',
  'Write times in JST (UTC+9) and say so.',
  'Customer business hours are 09:00–18:00 JST, Monday to Friday.',
  'Report configuration weaknesses with no sign of attack; do not ask the customer to confirm each one.',
].join('\n')

const bytes = (text: string) => new TextEncoder().encode(text).length

/** Settings › Operator instructions (#4918): one free-text value, appended to the system prompt of
 * every model call an agent run makes (investigations, hunts, root cause, compose phases, Ask Vigil).
 * Runs started after a save carry it; a run in progress keeps what it started with. */
export default function OperatorInstructionsSection({ notify }: SectionProps) {
  const { hasPermission } = useAuth()
  const canEdit = hasPermission('platform.write')
  const [saved, setSaved] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [max, setMax] = useState(8192)
  const [failed, setFailed] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let live = true
    configApi.getOperatorInstructions()
      .then(({ data }) => {
        if (!live) return
        setSaved(data.text)
        setDraft(data.text)
        setMax(data.max_bytes)
      })
      .catch((e) => { if (live) setFailed(errorText(e, 'Could not load the operator instructions. Reload to try again.')) })
    return () => { live = false }
  }, [])

  // the server trims before it checks the cap
  const used = bytes(draft.trim())
  const over = used > max
  const dirty = saved !== null && draft.trim() !== saved

  const save = async () => {
    setSaving(true)
    try {
      const { data } = await configApi.setOperatorInstructions(draft)
      setSaved(data.text)
      setDraft(data.text)
      notify('ok', data.text ? 'Operator instructions saved. Runs started from now on follow them.' : 'Operator instructions cleared.')
    } catch (e) {
      notify('err', errorText(e, 'Failed to save the operator instructions.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsCard
      title="Instructions for every agent"
      desc="Added to the end of every agent’s system prompt: investigations, hunts, root cause, workflow steps and Ask Vigil. A run keeps the text it started with."
      actions={canEdit && (
        <>
          {dirty && <button className="btn ghost" onClick={() => setDraft(saved ?? '')} disabled={saving}>Discard</button>}
          <button className="btn primary" onClick={save} disabled={!dirty || over || saving}>
            <Icon name="check" /> {saving ? 'Saving…' : 'Save'}
          </button>
        </>
      )}
    >
      {failed && (
        <div className="settings-banner err">
          <Icon name="alert" size={14} />
          <span>{failed}</span>
        </div>
      )}
      {saved === null && !failed && <p className="text-sm text-tx-3">Loading…</p>}
      {saved !== null && (
        <div className="oi-body">
          {saved === '' && (
            <div className="oi-empty">
              <Icon name="info" size={14} />
              <span>
                No instructions set, so agents write in English and state times in UTC. Use this for rules every case
                should follow, such as an output language, a time zone, customer business hours or an escalation policy.
                Rules for one case belong in that case; rules about one host or user, in its entity notes.
              </span>
            </div>
          )}
          <div className="field">
            <label className="field-label" htmlFor="oi-text">Instructions</label>
            <textarea
              id="oi-text"
              className="field-input oi-text"
              rows={10}
              value={draft}
              placeholder={PLACEHOLDER}
              readOnly={!canEdit}
              aria-describedby="oi-hint"
              onChange={(e) => setDraft(e.target.value)}
            />
            <span id="oi-hint" className={`field-hint${over ? ' err' : ''}`}>
              {used.toLocaleString()} of {max.toLocaleString()} bytes.{' '}
              {canEdit
                ? 'They change how agents write and what they emphasise. Tool grants, approvals and the autonomy ceiling are enforced whatever they say.'
                : 'Only an administrator can change these.'}
            </span>
          </div>
        </div>
      )}
    </SettingsCard>
  )
}
