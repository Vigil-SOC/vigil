import { useCallback, useEffect, useState } from 'react'
import { sessionPolicyApi, type SessionPolicy, type SessionPolicyBody } from '../../services/api'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { utcDayClock } from '../../shared/utc'
import { ConfirmDialog, NumberInput, Select, SettingsCard, Toggle } from '../../shared/ui'
import type { SectionProps } from './types'

type Rules = SessionPolicyBody['password_character_rules']
type Mfa = SessionPolicyBody['require_mfa']
type NumKey =
  | 'idle_standard_minutes' | 'idle_privileged_minutes' | 'absolute_hours' | 'sign_in_limit'
  | 'password_min_length' | 'password_history' | 'lockout_threshold' | 'lockout_minutes' | 'dormancy_days'
// What the form holds: every field of the body.
type Form = Record<NumKey, string> & { password_character_rules: Rules; require_mfa: Mfa; lockout_timed: boolean }

interface FieldDef {
  key: NumKey
  title: string
  hint: string
  unit: string
  /** empty is allowed and reads as `none` */
  optional?: boolean
  none?: string
}

const FIELDS: FieldDef[] = [
  {
    key: 'idle_standard_minutes', title: 'Idle limit', unit: 'min',
    hint: 'A sign-in with no action from the person for this long ends. Only clicks, keystrokes and page changes count; polling and live streams do not.',
  },
  {
    key: 'idle_privileged_minutes', title: 'Idle limit, privileged roles', unit: 'min',
    hint: 'Applies to a role that holds any of these permissions: users, roles, audit, integrations, AI autonomy, AI decision approval, sessions. It cannot exceed the idle limit.',
  },
  {
    key: 'absolute_hours', title: 'Sign-in length', unit: 'hours',
    hint: 'A sign-in ends this long after it started, however active it was. Service accounts have this limit only.',
  },
  {
    key: 'sign_in_limit', title: 'Open sign-ins per account', unit: 'sign-ins', optional: true, none: 'No limit',
    hint: 'The most sign-ins one account holds at once. At the limit, a new sign-in ends the oldest. Empty: no limit.',
  },
  {
    key: 'lockout_threshold', title: 'Failed sign-ins before a lock', unit: 'failures',
    hint: 'Failures in a row that lock an account. Anyone who knows a username can lock that person out. Without mail, only Unlock or the host reset script ends a lock that does not time out.',
  },
  {
    key: 'lockout_minutes', title: 'Timed lock length', unit: 'min',
    hint: 'How long a timed lock lasts. A person who keeps failing after it ends is locked again, and a long run of failures locks until unlocked.',
  },
  {
    key: 'dormancy_days', title: 'Dormancy period', unit: 'days', optional: true, none: 'Off',
    hint: 'Days without a sign-in or a personal MCP token’s use before an account is deactivated. Empty: off. An account no one uses stays open to anyone who learns its password. Longer periods, or off, leave such accounts open longer.',
  },
  {
    key: 'password_min_length', title: 'Password length', unit: 'characters',
    hint: 'The fewest characters a new password has. Applies when a password is set or changed; existing passwords are not rechecked.',
  },
  {
    key: 'password_history', title: 'Password history', unit: 'passwords',
    hint: 'How many previous passwords are kept. A new password cannot match any of them.',
  },
]

const RULES: { value: Rules; label: string; text: string }[] = [
  { value: 'none', label: 'Length only', text: 'length only' },
  { value: 'letters_digits', label: 'Letters and digits', text: 'letters and digits' },
  { value: 'four_classes', label: 'Four character classes', text: 'four character classes' },
]
const MFA: { value: Mfa; label: string; text: string }[] = [
  { value: 'off', label: 'Off', text: 'off' },
  { value: 'privileged', label: 'Privileged roles', text: 'privileged roles' },
  { value: 'everyone', label: 'Everyone', text: 'everyone' },
]
const MFA_TITLE = 'Require MFA'
const MFA_HINT = 'Whose sign-in needs two-step verification. Someone it covers who has none set up is held at setup until they do. Service accounts are never covered.'
const mfaText = (v: string | null | undefined) => MFA.find((m) => m.value === v)?.text ?? String(v)
const TIMED_TITLE = 'Timed locks'
const TIMED_HINT = 'On: a lock ends after the length set below. Off: a lock holds until an administrator unlocks the account or the person uses a reset link.'
const timedText = (v: boolean) => (v ? 'on' : 'off')

// Display order; the three that are not plain numbers have their own rows.
const ORDER: (NumKey | 'require_mfa' | 'lockout_timed' | 'password_character_rules')[] = [
  'idle_standard_minutes', 'idle_privileged_minutes', 'absolute_hours', 'sign_in_limit',
  'require_mfa', 'lockout_threshold', 'lockout_timed', 'lockout_minutes', 'dormancy_days',
  'password_min_length', 'password_history', 'password_character_rules',
]

const RULES_TITLE = 'Password character rules'
const RULES_HINT = 'What a new password must contain besides its length.'
const rulesText = (v: string | null | undefined) => RULES.find((r) => r.value === v)?.text ?? String(v)

const toForm = (p: SessionPolicy): Form => ({
  idle_standard_minutes: String(p.idle_standard_minutes),
  idle_privileged_minutes: String(p.idle_privileged_minutes),
  absolute_hours: String(p.absolute_hours),
  sign_in_limit: p.sign_in_limit == null ? '' : String(p.sign_in_limit),
  password_min_length: String(p.password_min_length),
  password_character_rules: p.password_character_rules,
  password_history: String(p.password_history),
  lockout_threshold: String(p.lockout_threshold),
  lockout_timed: p.lockout_timed,
  lockout_minutes: String(p.lockout_minutes),
  dormancy_days: p.dormancy_days == null ? '' : String(p.dormancy_days),
  require_mfa: p.require_mfa,
})

const shown = (f: FieldDef, v: number | null | undefined) => (v == null ? (f.none ?? 'None') : `${v} ${f.unit}`)

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
  const [form, setForm] = useState<Form | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [stale, setStale] = useState<SessionPolicy | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState<{ body: SessionPolicyBody; without_mfa: number | null } | null>(null)

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
    else if (f.key === 'lockout_minutes' && !form.lockout_timed) parsed[f.key] = saved.lockout_minutes
    else if (/^\d+$/.test(raw)) parsed[f.key] = Number(raw)
    else valid = false
  }
  const body = {
    ...parsed,
    password_character_rules: form.password_character_rules,
    lockout_timed: form.lockout_timed,
    require_mfa: form.require_mfa,
  } as SessionPolicyBody
  const was = toForm(saved)
  const dirty = (Object.keys(form) as (keyof Form)[]).some((k) => form[k] !== was[k])

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
      if ('loosening' in data && data.loosening) setConfirm({ body, without_mfa: data.without_mfa ?? null })
      else await save(body)
    } catch (e) {
      refused(e)
    } finally {
      setBusy(false)
    }
  }

  const next = confirm?.body
  const changes = next ? FIELDS.filter((f) => next[f.key] !== saved[f.key]) : []
  const rulesChanged = !!next && next.password_character_rules !== saved.password_character_rules
  const mfaChanged = !!next && next.require_mfa !== saved.require_mfa
  const timedChanged = !!next && next.lockout_timed !== saved.lockout_timed
  const range = (f: FieldDef) => saved.ranges[f.key]
  const dormancyLonger = !!next && saved.dormancy_days != null && (next.dormancy_days == null || next.dormancy_days > saved.dormancy_days)
  const set = (patch: Partial<Form>) => setForm({ ...form, ...patch })

  const numberRow = (f: FieldDef) => (
    <div className="lim-row" key={f.key}>
      <label className="lim-row-title" htmlFor={`sp-${f.key}`}>
        {f.title}
        <span>{f.hint}</span>
      </label>
      <span className="lim-row-tag">Saved: {shown(f, saved[f.key])} · allowed {range(f).min}–{range(f).max}</span>
      <span className="lim-row-ctl flex items-center gap-2">
        <NumberInput
          id={`sp-${f.key}`}
          style={{ width: 96 }}
          min={range(f).min}
          max={range(f).max}
          step={1}
          placeholder={f.optional ? 'None' : undefined}
          value={form[f.key]}
          disabled={busy}
          onChange={(e) => set({ [f.key]: e.target.value })}
        />
        <span className="text-xs text-tx-3">{f.unit}</span>
      </span>
    </div>
  )
  const mfaRow = (
    <div className="lim-row" key="require_mfa">
      <label className="lim-row-title" id="sp-mfa-label">
        {MFA_TITLE}
        <span>{MFA_HINT}</span>
      </label>
      <span className="lim-row-tag">Saved: {mfaText(saved.require_mfa)}</span>
      <span className="lim-row-ctl flex items-center gap-2">
        <Select
          value={form.require_mfa}
          options={MFA.map(({ value, label }) => ({ value, label }))}
          disabled={busy}
          onSelect={(v) => set({ require_mfa: v as Mfa })}
        />
      </span>
    </div>
  )
  const timedRow = (
    <div className="lim-row" key="lockout_timed">
      <span className="lim-row-title" id="sp-timed-label">
        {TIMED_TITLE}
        <span>{TIMED_HINT}</span>
      </span>
      <span className="lim-row-tag">Saved: {timedText(saved.lockout_timed)}</span>
      <span className="lim-row-ctl flex items-center gap-2">
        <Toggle checked={form.lockout_timed} label={TIMED_TITLE} disabled={busy} onChange={(v) => set({ lockout_timed: v })} />
      </span>
    </div>
  )
  const rulesRow = (
    <div className="lim-row" key="password_character_rules">
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
          onSelect={(v) => set({ password_character_rules: v as Rules })}
        />
      </span>
    </div>
  )
  return (
    <SettingsCard
      title="Sign-in limits, MFA, lockout and password rules"
      desc="How long a sign-in lasts, whose sign-in needs MFA, when failed sign-ins lock an account, when an unused one is deactivated, and what a new password must be. A saved change applies to every open sign-in at its next request."
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
                `require MFA ${mfaText(stale.require_mfa)}`,
                `${TIMED_TITLE.toLowerCase()} ${timedText(stale.lockout_timed)}`,
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
      {ORDER.map((key) => {
        if (key === 'require_mfa') return mfaRow
        if (key === 'lockout_timed') return timedRow
        if (key === 'password_character_rules') return rulesRow
        // The length applies only to timed locks.
        if (key === 'lockout_minutes' && !form.lockout_timed) return null
        return numberRow(FIELDS.find((f) => f.key === key)!)
      })}
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
        onConfirm={() => confirm && save(confirm.body)}
        onClose={() => setConfirm(null)}
        body={
          <>
            <p>This change loosens the policy: a sign-in can last longer, an account hold more sign-ins, a password be shorter, simpler or reused sooner, someone sign in without MFA, a guesser have more tries or a shorter lock, or an unused account stay open longer:</p>
            <ul className="my-2 pl-5 list-disc">
              {changes.map((f) => (
                <li key={f.key}>{f.title}: {shown(f, saved[f.key])} → {shown(f, next?.[f.key])}</li>
              ))}
              {mfaChanged && <li>{MFA_TITLE}: {mfaText(saved.require_mfa)} → {mfaText(next?.require_mfa)}</li>}
              {timedChanged && <li>{TIMED_TITLE}: {timedText(saved.lockout_timed)} → {timedText(next!.lockout_timed)}</li>}
              {rulesChanged && (
                <li>{RULES_TITLE}: {rulesText(saved.password_character_rules)} → {rulesText(next?.password_character_rules)}</li>
              )}
            </ul>
            {confirm?.without_mfa != null && (
              <p>{confirm.without_mfa} {confirm.without_mfa === 1 ? 'person' : 'people'} will be able to sign in without MFA.</p>
            )}
            {dormancyLonger && <p>Longer periods, or off, leave unused accounts open longer.</p>}
            <p>It is recorded in the audit trail with your name and the old and new values.</p>
          </>
        }
      />
    </SettingsCard>
  )
}
