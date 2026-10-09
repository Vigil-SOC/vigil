/* ============================================================
   Settings · AI models · Spending limit

   Bifrost enforces spend upstream of every LLM call, per virtual key. This
   card edits the one key Vigil presents as `x-bf-vk`: its ceiling, reset
   period and request rate. Bifrost owns the key and its limits; Vigil owns
   only which key it sends. Listing and deleting other keys is Bifrost's own UI.
   ============================================================ */
import { useEffect, useMemo, useState } from 'react'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { LevelBadge, type Level } from '../../shared/LevelBadge'
import {
  ConfirmDialog,
  EmptyState,
  Field,
  NumberInput,
  PasswordInput,
  Popup,
  Select,
  SettingsCard,
  TextInput,
} from '../../shared/ui'
import { useBudgets } from './useSettings'
import { useVirtualKeys, bifrostError } from './useBifrost'
import type { BifrostVirtualKey, BifrostVirtualKeyWrite } from '../../services/bifrostApi'
import type { SectionProps } from './types'

// Bifrost reads the long names back as short ones ("1mo"), so each period has aliases.
const PERIODS = [
  { value: 'daily', label: 'Daily', noun: 'day', days: 1, aliases: ['daily', '1d', '24h'] },
  { value: 'weekly', label: 'Weekly', noun: 'week', days: 7, aliases: ['weekly', '1w', '7d'] },
  { value: 'monthly', label: 'Monthly', noun: 'month', days: 30, aliases: ['monthly', '1mo', '1M', '30d'] },
]
const periodOf = (rd?: string) => PERIODS.find((p) => rd && p.aliases.includes(rd))

/** A masked value can't be used as a credential — see the paste field. */
const isMasked = (v?: string): boolean => !!v && v.includes('*')

/** The board's useLvl: >90% Poor, >=75% Fair, else Good. */
const levelOf = (pct: number): Level => (pct > 90 ? 'poor' : pct >= 75 ? 'fair' : 'good')

const money = (n: number) => `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`

/** Blank is "no limit"; anything else must be a positive number. */
const parseLimit = (raw: string): { value: number | null; valid: boolean } => {
  if (!raw.trim()) return { value: null, valid: true }
  const n = Number(raw)
  return n > 0 ? { value: n, valid: true } : { value: null, valid: false }
}

export default function AiBudgetsPanel({ notify }: SectionProps) {
  const { settings, quota, phase: vigilPhase, reload: reloadVigil, save } = useBudgets()
  const { vks, phase: vkPhase, error: vkError, reload: reloadVks, save: saveVk } = useVirtualKeys()
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)
  const [pasting, setPasting] = useState(false)
  const [secret, setSecret] = useState('')

  // The listed value is masked once a key has been created, so fall back to the
  // name the quota call reports for the key Bifrost resolved from default_vk.
  const key = useMemo(() => {
    const byValue = vks.find((v) => !!v.value && v.value === settings.default_vk)
    if (byValue || !settings.default_vk) return byValue
    const name = quota?.quota?.virtual_key_name
    return name ? vks.find((v) => v.name === name && (!v.value || isMasked(v.value))) : undefined
  }, [vks, settings.default_vk, quota])

  const persistVigilSide = async (default_vk: string) => {
    setBusy(true)
    try {
      // Round-trip the unused fields so a key change does not wipe the blob.
      await save({
        default_vk: default_vk.trim(),
        budget_limit_usd: settings.budget_limit_usd,
        enforcement_mode: settings.enforcement_mode,
      })
      notify('ok', 'Gateway key settings saved.')
      return true
    } catch (e) {
      notify('err', bifrostError(e, 'Save failed.'))
      return false
    } finally {
      setBusy(false)
    }
  }

  const retry = () => { reloadVigil(); reloadVks() }

  const pasteRow = (
    <div className="max-w-[560px]">
      <Field
        label="Virtual key (sk-bf-…)"
        hint="Bifrost shows a key's secret only once, at creation. Paste it here to point Vigil at a key made earlier."
      >
        <div className="flex items-center gap-2.5">
          <div className="grow">
            <PasswordInput value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="sk-bf-…" />
          </div>
          <button
            type="button"
            className="btn primary"
            disabled={busy || !secret.trim() || isMasked(secret)}
            onClick={async () => {
              if (await persistVigilSide(secret)) { setSecret(''); setPasting(false) }
            }}
          >
            <Icon name="check2" /> Use this key
          </button>
        </div>
      </Field>
    </div>
  )

  const loading =
    (vigilPhase === 'loading' && !quota) || (vkPhase === 'loading' && vks.length === 0)
  const failed = vigilPhase === 'error' || vkPhase === 'error'

  return (
    <SettingsCard
      wide
      title={
        <>
          Spending limit{' '}
          <InfoTip
            label="About metering"
            text="DEV_MODE=true or LLM_BUDGET_UNLIMITED=true omits the key from every call, so those calls are not metered."
            align="start"
          />
        </>
      }
      desc="The gateway checks this before any call, so a runaway agent is stopped rather than reported afterwards."
      actions={
        key && (
          <button className="btn ghost" onClick={() => setPasting((p) => !p)}>
            <Icon name="edit" /> Use a different key
          </button>
        )
      }
    >
      {loading && <EmptyState loading compact icon="sparkle" title="Loading spending limit…" />}
      {!loading && failed && (
        <EmptyState
          error
          compact
          icon="alert"
          title="Couldn’t load the spending limit"
          body={vkPhase === 'error' ? vkError : 'The budget settings did not load.'}
          primary={{ label: 'Retry', onClick: retry, icon: 'refresh' }}
        />
      )}
      {!loading && !failed && (
        <div className="flex flex-col gap-4">
          {quota?.configured && !quota.available && (
            <div className="settings-banner err">
              <Icon name="alert" size={14} /> {quota.message || 'The configured key does not exist on the gateway.'}
            </div>
          )}
          {key ? (
            <LimitForm
              key={key.id}
              vk={key}
              live={quota?.available ? quota.quota?.budgets?.[0] : undefined}
              busy={busy}
              onSave={async (data) => {
                setBusy(true)
                try {
                  await saveVk(key.id, data)
                  reloadVigil()
                  notify('ok', 'Spending limit saved.')
                } catch (e) {
                  notify('err', bifrostError(e, 'Save failed.'))
                } finally {
                  setBusy(false)
                }
              }}
            />
          ) : (
            <EmptyState
              compact
              icon="sparkle"
              title={quota?.configured ? 'Vigil’s key can’t be edited here' : 'No spending limit yet'}
              body={
                quota?.configured
                  ? 'Bifrost doesn’t list this key’s secret, so Vigil can’t tell which one it is. Paste its secret below, or create a new key.'
                  : quota?.message || 'No key configured — calls run unmetered. Create a key with a ceiling and Vigil will send it on every call.'
              }
              primary={{ label: 'New key', onClick: () => setCreating(true), icon: 'plus' }}
            />
          )}
          {(!key || pasting) && pasteRow}
        </div>
      )}

      {creating && (
        <NewKeyDialog
          onClose={() => setCreating(false)}
          onSave={async (data) => {
            const saved = await saveVk(null, data)
            setCreating(false)
            // The secret is shown once and never again, so it is put to use in
            // the same breath rather than left for the operator to copy out.
            if (saved?.value && !isMasked(saved.value)) {
              if (await persistVigilSide(saved.value)) notify('ok', `Created ${saved.name} and pointed Vigil at it.`)
            } else {
              notify('ok', 'Virtual key created. Paste its secret to point Vigil at it.')
            }
          }}
        />
      )}
    </SettingsCard>
  )
}

/* ---------------- The one key Vigil sends ---------------- */
function LimitForm({ vk, live, busy, onSave }: {
  vk: BifrostVirtualKey
  live?: { max_limit: number; current_usage: number; reset_duration: string }
  busy: boolean
  onSave: (data: BifrostVirtualKeyWrite) => Promise<void>
}) {
  const stored = vk.budget
  const storedRpm = vk.rate_limit?.request_max_limit
  const initial = useMemo(() => ({
    ceiling: stored?.max_limit ? String(stored.max_limit) : '',
    reset: periodOf(stored?.reset_duration)?.value || stored?.reset_duration || 'monthly',
    rpm: storedRpm ? String(storedRpm) : '',
  }), [stored?.max_limit, stored?.reset_duration, storedRpm])
  const [draft, setDraft] = useState(initial)
  const [confirming, setConfirming] = useState<BifrostVirtualKeyWrite | null>(null)
  useEffect(() => setDraft(initial), [initial])

  const ceiling = parseLimit(draft.ceiling)
  const rpm = parseLimit(draft.rpm)
  // compared as numbers, and the period only counts while there is a ceiling to reset
  const dirty =
    ceiling.value !== (stored?.max_limit || null) ||
    rpm.value !== (storedRpm || null) ||
    (!!ceiling.value && draft.reset !== initial.reset) ||
    !ceiling.valid ||
    !rpm.valid
  // an unrecognised stored period ("6h") stays selectable so a save can't rewrite it
  const resetOptions = periodOf(draft.reset) || !draft.reset
    ? PERIODS
    : [...PERIODS, { value: draft.reset, label: draft.reset }]

  const build = (): BifrostVirtualKeyWrite => ({
    // everything the card does not draw is sent back as it was read
    name: vk.name,
    description: vk.description,
    is_active: vk.is_active,
    allowed_models: vk.allowed_models,
    allowed_providers: vk.allowed_providers,
    budget: ceiling.value ? { max_limit: ceiling.value, reset_duration: draft.reset } : null,
    rate_limit:
      rpm.value || vk.rate_limit?.token_max_limit
        ? {
            // an unchanged limit keeps the window it was stored with; a new one is per minute
            ...(rpm.value
              ? {
                  request_max_limit: rpm.value,
                  request_reset_duration:
                    rpm.value === storedRpm ? vk.rate_limit?.request_reset_duration || 'minute' : 'minute',
                }
              : {}),
            ...(vk.rate_limit?.token_max_limit
              ? {
                  token_max_limit: vk.rate_limit.token_max_limit,
                  token_reset_duration: vk.rate_limit.token_reset_duration,
                }
              : {}),
          }
        : null,
  })

  // Raising or removing a ceiling, or stretching it over a shorter period, loosens
  // the one thing that stops a runaway agent.
  const perDay = (amount: number, reset?: string) => amount / (periodOf(reset)?.days ?? 1)
  const loosens =
    !!stored?.max_limit &&
    (!ceiling.value ||
      perDay(ceiling.value, draft.reset) > perDay(stored.max_limit, stored.reset_duration))

  const used = live?.current_usage ?? stored?.current_usage ?? 0
  const max = live?.max_limit ?? stored?.max_limit ?? 0
  const pct = max > 0 ? Math.min(100, Math.round((used / max) * 100)) : 0
  const level = levelOf(pct)
  const noun = periodOf(stored?.reset_duration || live?.reset_duration)?.noun

  return (
    <>
      <div className="settings-grid-2" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))' }}>
        <Field
          label="Spend ceiling (USD)"
          error={ceiling.valid ? null : 'Enter an amount above 0, or clear it for no ceiling.'}
        >
          <NumberInput
            value={draft.ceiling}
            min={0}
            placeholder="No ceiling"
            onChange={(e) => setDraft({ ...draft, ceiling: e.target.value })}
          />
        </Field>
        <Field label="Resets">
          <Select value={draft.reset} options={resetOptions} onSelect={(reset) => setDraft({ ...draft, reset })} />
        </Field>
        <Field
          label="Requests per minute"
          error={rpm.valid ? null : 'Enter a number above 0, or clear it for no limit.'}
        >
          <NumberInput
            value={draft.rpm}
            min={0}
            placeholder="No limit"
            onChange={(e) => setDraft({ ...draft, rpm: e.target.value })}
          />
        </Field>
      </div>
      <p className="text-xs text-tx-3">
        At the ceiling Bifrost refuses further LLM calls (HTTP 402) until the period resets — it does not just warn.
      </p>

      {max > 0 ? (
        <div>
          <div className="flex items-center justify-between gap-3 mb-1.5 text-sm">
            <span className="font-medium">Used this {noun || 'period'}</span>
            <span className="flex items-center gap-2">
              <span className="text-tx-2">{money(used)} of {money(max)}</span>
              <LevelBadge variant="pill" level={level} />
              <span className="text-xs text-tx-3">{pct}%</span>
            </span>
          </div>
          <div className="h-1.5 rounded-full bg-[var(--bg-3)] overflow-hidden" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Spend used">
            <div className="h-full rounded-full" style={{ width: `${pct}%`, background: `var(--${level})` }} />
          </div>
        </div>
      ) : (
        <div className="settings-banner info">
          <Icon name="info" size={14} /> This key is unmetered: Bifrost will not stop a runaway loop. Set a spend ceiling above to meter it.
        </div>
      )}

      <div>
        <button
          className="btn primary"
          disabled={busy || !dirty || !ceiling.valid || !rpm.valid}
          onClick={() => (loosens ? setConfirming(build()) : onSave(build()))}
        >
          <Icon name="check2" /> {busy ? 'Saving…' : 'Save'}
        </button>
      </div>

      <ConfirmDialog
        open={!!confirming}
        title={confirming?.budget ? 'Raise the spend ceiling?' : 'Remove the spend ceiling?'}
        body={
          confirming?.budget
            ? `This lets Vigil spend up to ${money(confirming.budget.max_limit)} per ${periodOf(confirming.budget.reset_duration)?.noun || 'period'} instead of ${money(stored?.max_limit || 0)} per ${noun || 'period'}.`
            : 'With no ceiling, calls run unmetered and Bifrost will not stop a runaway agent.'
        }
        confirmLabel={confirming?.budget ? 'Raise ceiling' : 'Remove ceiling'}
        busy={busy}
        onConfirm={async () => {
          if (confirming) await onSave(confirming)
          setConfirming(null)
        }}
        onClose={() => setConfirming(null)}
      />
    </>
  )
}

/* ---------------- New key ---------------- */
function NewKeyDialog({ onClose, onSave }: {
  onClose: () => void
  onSave: (data: BifrostVirtualKeyWrite) => Promise<void>
}) {
  const [name, setName] = useState('vigil-soc')
  const [ceiling, setCeiling] = useState('100')
  const [reset, setReset] = useState('monthly')
  const [rpm, setRpm] = useState('')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const cap = parseLimit(ceiling)
  const rate = parseLimit(rpm)

  const submit = async () => {
    setSaving(true)
    setErr(null)
    try {
      await onSave({
        name: name.trim(),
        is_active: true,
        budget: cap.value ? { max_limit: cap.value, reset_duration: reset } : null,
        rate_limit: rate.value ? { request_max_limit: rate.value, request_reset_duration: 'minute' } : null,
      })
    } catch (e) {
      setErr(bifrostError(e, 'Save failed.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Popup open onClose={onClose} title="New virtual key">
      {err && <div className="settings-banner err mb-3"><Icon name="alert" size={14} /> {err}</div>}
      <div className="flex flex-col gap-3.5">
        <Field label="Name" hint="How this key appears in the gateway's logs and cost breakdowns.">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="vigil-soc" />
        </Field>
        <div className="settings-grid-2" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)' }}>
          <Field label="Spend ceiling (USD)" hint="Blank means unmetered." error={cap.valid ? null : 'Enter an amount above 0.'}>
            <NumberInput value={ceiling} min={0} onChange={(e) => setCeiling(e.target.value)} />
          </Field>
          <Field label="Resets">
            <Select value={reset} options={PERIODS} onSelect={setReset} />
          </Field>
        </div>
        <Field label="Requests per minute" hint="Blank means no limit." error={rate.valid ? null : 'Enter a number above 0.'}>
          <NumberInput value={rpm} min={0} onChange={(e) => setRpm(e.target.value)} />
        </Field>
      </div>
      <div className="flex justify-end gap-2.5 mt-5">
        <button className="btn ghost" onClick={onClose} disabled={saving}>Cancel</button>
        <button className="btn primary" disabled={saving || !name.trim() || !cap.valid || !rate.valid} onClick={submit}>
          <Icon name="check2" /> {saving ? 'Saving…' : 'Create and use'}
        </button>
      </div>
    </Popup>
  )
}
