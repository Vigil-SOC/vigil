// Records one known-activity rule. The case panel offers the entities its alerts name;
// Settings takes any entity. The date opens on the server's default.
import { useState, type FormEvent } from 'react'
import { knownActivityApi, type KnownActivity, type KnownActivityOrigin, type NamedEntity } from '../services/api'
import { errorText } from './errorText'
import { Field, Select, TextInput } from './ui'
import './knownActivity.css'

export const kindLabel = (kind: string): string => (kind === 'ip' ? 'IP' : kind.replace(/_/g, ' '))

const KNOWN_ACTIVITY_HINT = 'This explains the alert to people and agents. It hides nothing and changes nothing.'

type Choices = { entities: NamedEntity[]; kinds?: never } | { kinds: string[]; entities?: never }

export function KnownActivityForm({ entities, kinds, defaultExpiresAt, origin = 'ad_hoc', originRef, onSaved, onCancel }: Choices & {
  defaultExpiresAt: string
  origin?: KnownActivityOrigin
  originRef?: string
  onSaved: (rule: KnownActivity) => void
  onCancel: () => void
}) {
  const [picked, setPicked] = useState('0')
  const [kind, setKind] = useState(kinds?.[0] ?? '')
  const [value, setValue] = useState('')
  const [reason, setReason] = useState('')
  const [until, setUntil] = useState(defaultExpiresAt.slice(0, 10))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const chosen: NamedEntity = entities ? entities[Number(picked)] : { entity_kind: kind, entity_value: value.trim() }
  const ready = !!chosen?.entity_value && reason.trim() !== '' && until !== '' && !saving

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!ready) return
    setSaving(true)
    setError('')
    try {
      const made = await knownActivityApi.create({
        ...chosen,
        reason: reason.trim(),
        // Through the end of the day picked, UTC, as the console shows dates.
        expires_at: `${until}T23:59:59Z`,
        origin,
        origin_ref: originRef,
      })
      onSaved(made.data)
    } catch (e) {
      setError(errorText(e, 'Failed to record known activity'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="ka-form" onSubmit={submit} aria-label="Record known activity">
      <div className="ka-form-grid">
        {entities ? (
          <div className="field">
            <span className="field-label">Entity</span>
            <Select
              value={picked}
              options={entities.map((e, i) => ({ value: String(i), label: `${kindLabel(e.entity_kind)} · ${e.entity_value}` }))}
              onSelect={setPicked}
            />
          </div>
        ) : (
          <>
            <div className="field">
              <span className="field-label">Kind</span>
              <Select value={kind} options={kinds.map((k) => ({ value: k, label: kindLabel(k) }))} onSelect={setKind} />
            </div>
            <Field label="Entity">
              <TextInput value={value} onChange={(e) => setValue(e.target.value)} placeholder="The value alerts name" />
            </Field>
          </>
        )}
        <Field label="Until (UTC)">
          <input type="date" className="field-input" value={until} onChange={(e) => setUntil(e.target.value)} required />
        </Field>
      </div>
      <Field label="Why this is expected" hint={KNOWN_ACTIVITY_HINT} error={error || null}>
        <TextInput value={reason} onChange={(e) => setReason(e.target.value)} placeholder="What it does, and why that is fine" />
      </Field>
      <div className="ka-form-actions">
        <button type="submit" className="btn primary" disabled={!ready}>{saving ? 'Recording…' : 'Record'}</button>
        <button type="button" className="btn ghost" onClick={onCancel} disabled={saving}>Cancel</button>
      </div>
    </form>
  )
}
