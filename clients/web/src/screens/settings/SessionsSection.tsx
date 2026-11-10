import { useCallback, useEffect, useState } from 'react'
import { sessionPolicyApi, type SessionPolicy, type SessionPolicyBody } from '../../services/api'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { utcDayClock } from '../../shared/utc'
import { ConfirmDialog, NumberInput, Select, SettingsCard } from '../../shared/ui'
import type { SectionProps } from './types'

// The lockout and dormancy fields have no control here: a save sends the saved ones back unchanged.
type Key = Exclude<keyof SessionPolicyBody, 'lockout_threshold' | 'lockout_timed' | 'lockout_minutes' | 'dormancy_days'>
type NumKey = Exclude<Key, 'password_character_rules'>
type Rules = SessionPolicyBody['password_character_rules']

interface FieldDef {
  key: NumKey
  title: string
  hint: string
  unit: string
  min: number
  max: number
  /** empty means no limit */
  optional?: boolean
}

const FIELDS: FieldDef[] = [
  {
    key: 'idle_standard_minutes', title: 'Idle limit', unit: 'min', min: 1, max: 60,
    hint: 'A sign-in with no action from the person for this long ends. Only clicks, keystrokes and page changes count; polling and live streams do not.',
  },
  {
    key: 'idle_privileged_minutes', title: 'Idle limit, privileged roles', unit: 'min', min: 1, max: 30,
    hint: 'Applies to a role that holds any of these permissions: users, roles, audit, integrations, AI autonomy, AI decision approval, sessions. It cannot exceed the idle limit.',
  },
  {
    key: 'absolute_hours', title: 'Sign-in length', unit: 'hours', min: 1, max: 24,
    hint: 'A sign-in ends this long after it started, however active it was. Service accounts have this limit only.',
  },
  {
    key: 'sign_in_limit', title: 'Open sign-ins per account', unit: 'sign-ins', min: 1, max: 10, optional: true,
    hint: 'The most sign-ins one account holds at once. At the limit, a new sign-in ends the oldest. Empty: no limit.',
  },
  {
    key: 'password_min_length', title: 'Password length', unit: 'characters', min: 15, max: 64,
    hint: 'The fewest characters a new password has. Applies when a password is set or changed; existing passwords are not rechecked.',
  },
  {
    key: 'password_history', title: 'Password history', unit: 'passwords', min: 4, max: 24,
    hint: 'How many previous passwords are kept. A new password cannot match any of them.',
  },
]

const RULES: { value: Rules; label: string; text: string }[] = [
  { value: 'none', label: 'Length only', text: 'length only' },
  { value: 'letters_digits', label: 'Letters and digits', text: 'letters and digits' },
  { value: 'four_classes', label: 'Four character classes', text: 'four character classes' },
]
const RULES_TITLE = 'Password character rules'
const RULES_HINT = 'What a new password must contain besides its length.'
const rulesText = (v: string | null | undefined) => RULES.find((r) => r.value === v)?.text ?? String(v)

const toForm = (p: SessionPolicy): Record<Key, string> => ({
  idle_standard_minutes: String(p.idle_standard_minutes),
  idle_privileged_minutes: String(p.idle_privileged_minutes),
  absolute_hours: String(p.absolute_hours),
  sign_in_limit: p.sign_in_limit == null ? '' : String(p.sign_in_limit),
  password_min_length: String(p.password_min_length),
  password_character_rules: p.password_character_rules,
  password_history: String(p.password_history),
})

const shown = (f: FieldDef, v: number | null | undefined) => (v == null ? 'No limit' : `${v} ${f.unit}`)

// Validation messages arrive as a list; the server's own wording is what is shown.
function failure(e: unknown, fallback: string) {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => String((d as { msg?: unknown })?.msg ?? '').replace(/^Value error, /, '')).filter(Boolean)
    if (msgs.length) return msgs.join(' ')
  }
  return errorText(e, fallback)
}

const who = (p: SessionPolicy) => {
  const when = utcDayClock(p.updated_at)
  return p.version === 0 ? 'Never saved: these are the defaults.' : `Version ${p.version}, changed by ${p.updated_by_name ?? 'a removed account'}${when ? ` on ${when} UTC` : ''}.`
}

/** Settings › Sessions: the session policy (GET/PUT /api/session-policy). A save sends the version
 * it was read at in If-Match; a 412 shows what is saved now and who changed it, and saves nothing.
 * Whether a change loosens the policy is the server's answer (a dry run), never worked out here. */
export default function SessionsSection({ notify }: SectionProps) {
  const [saved, setSaved] = useState<SessionPolicy | null>(null)
  const [form, setForm] = useState<Record<Key, string> | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [stale, setStale] = useState<SessionPolicy | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState<SessionPolicyBody | null>(null)

  const load = useCallback(() => {
    setFailed(null)
    sessionPolicyApi.get()
      .then((res) => { setSaved(res.data); setForm(toForm(res.data)); setStale(null); setProblem(null) })
      .catch((e) => setFailed(failure(e, 'Could not load the session policy.')))
  }, [])
  useEffect(load, [load])

  if (failed) {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">{failed}</span>
        <button className="btn ghost" onClick={load}>Retry</button>
      </div>
    )
  }
  if (!saved || !form) return <div className="text-sm text-tx-3 py-16 text-center">Loading session policy…</div>

  const parsed: Partial<Record<NumKey, number | null>> = {}
  let valid = true
  for (const f of FIELDS) {
    const raw = form[f.key].trim()
    if (raw === '' && f.optional) parsed[f.key] = null
    else if (/^\d+$/.test(raw)) parsed[f.key] = Number(raw)
    else valid = false
  }
  const { lockout_threshold, lockout_timed, lockout_minutes, dormancy_days } = saved
  const body = {
    ...parsed,
    password_character_rules: form.password_character_rules as Rules,
    lockout_threshold,
    lockout_timed,
    lockout_minutes,
    dormancy_days,
  } as SessionPolicyBody
  const dirty = FIELDS.some((f) => form[f.key] !== toForm(saved)[f.key])

  const save = async (next: SessionPolicyBody) => {
    setBusy(true)
    setProblem(null)
    try {
      const res = await sessionPolicyApi.put(next, saved.version)
      setSaved(res.data)
      setForm(toForm(res.data))
      setStale(null)
      notify('ok', 'Session policy saved. It applies at each open sign-in’s next request.')
    } catch (e) {
      refused(e)
    } finally {
      setBusy(false)
      setConfirm(null)
    }
  }

  // A 412 carries what is saved now; anything else is the server's own message.
  const refused = (e: unknown) => {
    const data = (e as { response?: { status?: number; data?: { current?: SessionPolicy } } })?.response
    if (data?.status === 412 && data.data?.current) setStale(data.data.current)
    else setProblem(failure(e, 'The session policy was not saved.'))
  }

  const submit = async () => {
    setBusy(true)
    setProblem(null)
    try {
      const { data } = await sessionPolicyApi.loosens(body, saved.version)
      if ('loosening' in data && data.loosening) setConfirm(body)
      else await save(body)
    } catch (e) {
      refused(e)
    } finally {
      setBusy(false)
    }
  }

  const changes = confirm ? FIELDS.filter((f) => confirm[f.key] !== saved[f.key]) : []
  const rulesChanged = !!confirm && confirm.password_character_rules !== saved.password_character_rules
  return (
    <SettingsCard
      title="Sign-in limits and password rules"
      desc="How long a sign-in lasts and what a new password must be. A saved change applies to every open sign-in at its next request."
    >
      {stale && (
        <div className="settings-banner err mb-3" role="alert" style={{ alignItems: 'flex-start' }}>
          <Icon name="alert" size={14} />
          <div className="flex flex-col gap-1.5">
            <span>
              The policy changed after you opened it, so nothing was saved. {who(stale)}
            </span>
            <span>
              Now: {[
                ...FIELDS.map((f) => `${f.title.toLowerCase()} ${shown(f, stale[f.key])}`),
                `${RULES_TITLE.toLowerCase()} ${rulesText(stale.password_character_rules)}`,
              ].join(' · ')}.
            </span>
            <span>
              <button className="btn ghost" onClick={() => { setSaved(stale); setForm(toForm(stale)); setStale(null) }}>
                Load the current values
              </button>
            </span>
          </div>
        </div>
      )}
      {problem && (
        <div className="settings-banner err mb-3" role="alert">
          <Icon name="alert" size={14} />
          <span>{problem}</span>
        </div>
      )}
      {FIELDS.map((f) => (
        <div className="lim-row" key={f.key}>
          <label className="lim-row-title" htmlFor={`sp-${f.key}`}>
            {f.title}
            <span>{f.hint}</span>
          </label>
          <span className="lim-row-tag">Saved: {shown(f, saved[f.key])} · allowed {f.min}–{f.max}</span>
          <span className="lim-row-ctl flex items-center gap-2">
            <NumberInput
              id={`sp-${f.key}`}
              style={{ width: 96 }}
              min={f.min}
              max={f.max}
              step={1}
              placeholder={f.optional ? 'None' : undefined}
              value={form[f.key]}
              disabled={busy}
              onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
            />
            <span className="text-xs text-tx-3">{f.unit}</span>
          </span>
        </div>
      ))}
      <div className="lim-row">
        <label className="lim-row-title" id="sp-rules-label">
          {RULES_TITLE}
          <span>{RULES_HINT}</span>
        </label>
        <span className="lim-row-tag">Saved: {rulesText(saved.password_character_rules)}</span>
        <span className="lim-row-ctl flex items-center gap-2">
          <Select
            value={form.password_character_rules}
            options={RULES.map(({ value, label }) => ({ value, label }))}
            disabled={busy}
            onSelect={(v) => setForm({ ...form, password_character_rules: v })}
          />
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2.5 mt-3.5">
        <button className="btn primary" disabled={busy || !dirty || !valid} onClick={submit}>
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button className="btn ghost" disabled={busy || !dirty} onClick={() => { setForm(toForm(saved)); setProblem(null) }}>
          Discard changes
        </button>
        <span className="text-xs text-tx-3">{who(saved)}</span>
      </div>
      <ConfirmDialog
        open={!!confirm}
        danger={false}
        title="Loosen the policy?"
        confirmLabel="Save change"
        busy={busy}
        onConfirm={() => confirm && save(confirm)}
        onClose={() => setConfirm(null)}
        body={
          <>
            <p>This change lets a sign-in last longer, an account hold more sign-ins, or a password be shorter, simpler or reused sooner:</p>
            <ul className="my-2 pl-5 list-disc">
              {changes.map((f) => (
                <li key={f.key}>{f.title}: {shown(f, saved[f.key])} → {shown(f, confirm?.[f.key])}</li>
              ))}
              {rulesChanged && (
                <li>{RULES_TITLE}: {rulesText(saved.password_character_rules)} → {rulesText(confirm?.password_character_rules)}</li>
              )}
            </ul>
            <p>It is recorded in the audit trail with your name and the old and new values.</p>
          </>
        }
      />
    </SettingsCard>
  )
}
