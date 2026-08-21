/* ============================================================
   Settings · AI Config · Budgets & Virtual Keys

   Bifrost enforces spend upstream of every LLM call, per virtual key. This
   panel manages those keys here rather than sending the operator to Bifrost's
   own UI to provision one and paste its id back.

   The split of ownership: Bifrost owns the keys, their budgets and their rate
   limits. Vigil owns only which key it presents as `x-bf-vk`, and what to do
   when the gateway says no.
   ============================================================ */
import { useEffect, useState } from 'react'
import { Icon } from '../../shared/icons'
import {
  ConfirmDialog,
  EmptyState,
  Field,
  NumberInput,
  Popup,
  Select,
  SettingsCard,
  TextInput,
  Toggle,
} from '../../shared/ui'
import { useBudgets } from './useSettings'
import { useVirtualKeys, bifrostError } from './useBifrost'
import type { BifrostVirtualKey, BifrostVirtualKeyWrite } from '../../../services/bifrostApi'
import type { SectionProps } from './types'

const ENFORCEMENT_OPTIONS = [
  { value: 'warning', label: 'Warning only — log but allow' },
  { value: 'hard_stop', label: 'Hard stop — block on exceed' },
]

const RESET_OPTIONS = [
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
]

function maskVk(vk: string): string {
  if (!vk || vk.length <= 8) return vk
  return `${vk.slice(0, 6)}…${vk.slice(-4)}`
}

/** A masked value can't be used as a credential — see the note on the paste field. */
const isMasked = (v?: string): boolean => !!v && v.includes('*')

function SpendBar({ used, limit }: { used: number; limit: number }) {
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0
  const color = pct >= 90 ? 'var(--crit)' : pct >= 75 ? 'var(--high)' : 'var(--accent)'
  return (
    <div style={{ minWidth: 130 }}>
      <div className="text-xs mb-1">
        ${used.toFixed(2)} / ${limit.toFixed(2)}
      </div>
      <div className="h-1.5 rounded-full bg-[var(--bg-3)] overflow-hidden">
        <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  )
}

export default function AiBudgetsPanel({ notify }: SectionProps) {
  const { settings, quota, phase: vigilPhase, save } = useBudgets()
  const { vks, phase, error, reload, save: saveVk, remove } = useVirtualKeys()
  const [editing, setEditing] = useState<{ vk: BifrostVirtualKey | null } | null>(null)
  const [confirmDel, setConfirmDel] = useState<BifrostVirtualKey | null>(null)
  const [busy, setBusy] = useState(false)
  const [draftVk, setDraftVk] = useState(settings.default_vk)
  const [showVk, setShowVk] = useState(false)
  const [enforcement, setEnforcement] = useState(settings.enforcement_mode)

  useEffect(() => {
    setDraftVk(settings.default_vk)
    setEnforcement(settings.enforcement_mode)
  }, [settings])

  const persistVigilSide = async (default_vk: string, mode: typeof settings.enforcement_mode) => {
    setBusy(true)
    try {
      await save({
        default_vk: default_vk.trim(),
        budget_limit_usd: settings.budget_limit_usd,
        enforcement_mode: mode,
      })
      notify('ok', 'Gateway key settings saved.')
    } catch (e) {
      notify('err', bifrostError(e, 'Save failed.'))
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async () => {
    if (!confirmDel) return
    setBusy(true)
    try {
      await remove(confirmDel.id)
      notify('ok', `Deleted virtual key ${confirmDel.name}.`)
      setConfirmDel(null)
    } catch (e) {
      notify('err', bifrostError(e, 'Delete failed.'))
    } finally {
      setBusy(false)
    }
  }

  const activeVk = settings.default_vk
  const liveBudget = quota?.quota?.budgets?.[0]

  return (
    <>
      <SettingsCard
        wide
        title="Virtual Keys"
        desc="Bifrost checks the budget on a virtual key before it spends anything upstream, so a ceiling here stops a runaway agent rather than reporting it afterwards."
        actions={
          <>
            <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
            <button className="btn primary" onClick={() => setEditing({ vk: null })}>
              <Icon name="plus" /> New key
            </button>
          </>
        }
      >
        {phase === 'loading' && <EmptyState loading compact icon="sparkle" title="Loading virtual keys…" />}
        {phase === 'error' && (
          <EmptyState error compact icon="alert" title="Couldn’t load virtual keys" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
        )}
        {phase === 'ready' && vks.length === 0 && (
          <EmptyState
            compact
            icon="sparkle"
            title="No virtual keys"
            body="Without one, calls run unmetered and no budget is enforced. Create a key with a monthly ceiling and point Vigil at it below."
            primary={{ label: 'New key', onClick: () => setEditing({ vk: null }), icon: 'plus' }}
          />
        )}
        {phase === 'ready' && vks.length > 0 && (
          <div className="table-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Name</th><th>Budget</th><th>Rate limits</th><th>Status</th><th>Used by Vigil</th>
                  <th style={{ textAlign: 'right' }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {vks.map((vk) => {
                  const isActive = !!vk.value && vk.value === activeVk
                  const budget = isActive && liveBudget ? liveBudget : vk.budget
                  return (
                    <tr key={vk.id}>
                      <td>
                        <div className="flex flex-col">
                          <span>{vk.name}</span>
                          {vk.description && <span className="text-xs text-tx-3">{vk.description}</span>}
                        </div>
                      </td>
                      <td>
                        {budget && budget.max_limit > 0 ? (
                          <SpendBar used={budget.current_usage || 0} limit={budget.max_limit} />
                        ) : (
                          <span className="text-xs text-tx-3">Unlimited</span>
                        )}
                      </td>
                      <td className="text-xs">
                        {vk.rate_limit?.request_max_limit
                          ? `${vk.rate_limit.request_max_limit} req/${vk.rate_limit.request_reset_duration || 'min'}`
                          : '—'}
                        {vk.rate_limit?.token_max_limit
                          ? ` · ${vk.rate_limit.token_max_limit.toLocaleString()} tok/${vk.rate_limit.token_reset_duration || 'min'}`
                          : ''}
                      </td>
                      <td>
                        {vk.is_active
                          ? <span className="status closed">Active</span>
                          : <span className="chip">Disabled</span>}
                      </td>
                      <td>
                        {isActive ? (
                          <span className="chip" style={{ color: 'var(--accent-2)' }}>In use</span>
                        ) : (
                          <button
                            className="btn ghost"
                            disabled={busy || !vk.value || isMasked(vk.value)}
                            title={
                              !vk.value || isMasked(vk.value)
                                ? 'Bifrost only returns a key’s secret once, at creation. Paste it into the field below to use this key.'
                                : 'Send this key on every LLM call'
                            }
                            onClick={() => persistVigilSide(vk.value || '', enforcement)}
                          >
                            Use
                          </button>
                        )}
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <div className="inline-flex gap-1.5">
                          <button className="btn ghost icon" title="Edit" onClick={() => setEditing({ vk })}>
                            <Icon name="edit" size={15} />
                          </button>
                          <button className="btn ghost icon" title="Delete" onClick={() => setConfirmDel(vk)}>
                            <Icon name="trash" size={15} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </SettingsCard>

      <SettingsCard
        title="What Vigil sends"
        desc="The key presented as x-bf-vk on every upstream call, and what to do when the gateway refuses one. DEV_MODE=true or LLM_BUDGET_UNLIMITED=true bypasses enforcement entirely."
      >
        {vigilPhase === 'loading' ? (
          <div className="text-sm text-tx-3 py-6 text-center">Loading…</div>
        ) : (
          <div className="flex flex-col gap-3.5 max-w-[560px]">
            {quota?.configured && !quota.available && (
              <div className="settings-banner err"><Icon name="alert" size={14} /> {quota.message || 'The configured key does not exist on the gateway.'}</div>
            )}
            {!quota?.configured && (
              <div className="settings-banner info"><Icon name="info" size={14} /> {quota?.message || 'No key configured — calls run unmetered.'}</div>
            )}
            <Field
              label="Virtual key (sk-bf-…)"
              hint="Set automatically by “Use” above. Paste manually for a key created earlier — Bifrost reveals a key's secret only once, at creation."
            >
              <TextInput
                value={showVk ? draftVk : maskVk(draftVk)}
                onFocus={() => setShowVk(true)}
                onBlur={() => setShowVk(false)}
                onChange={(e) => setDraftVk(e.target.value)}
              />
            </Field>
            <Field label="Enforcement mode">
              <Select
                value={enforcement}
                options={ENFORCEMENT_OPTIONS}
                onSelect={(v) => setEnforcement(v as typeof enforcement)}
              />
            </Field>
            <div>
              <button
                className="btn primary"
                disabled={busy || (draftVk === settings.default_vk && enforcement === settings.enforcement_mode)}
                onClick={() => persistVigilSide(draftVk, enforcement)}
              >
                <Icon name="check2" /> {busy ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        )}
      </SettingsCard>

      {editing && (
        <VirtualKeyDialog
          existing={editing.vk}
          onClose={() => setEditing(null)}
          onSave={async (data) => {
            const saved = await saveVk(editing.vk?.id || null, data)
            setEditing(null)
            // A new key's secret is shown once and never again, so it has to be
            // put to use in the same breath rather than left for the operator
            // to copy out of a toast.
            if (!editing.vk && saved?.value && !isMasked(saved.value)) {
              await persistVigilSide(saved.value, enforcement)
              notify('ok', `Created ${saved.name} and pointed Vigil at it.`)
            } else {
              notify('ok', 'Virtual key saved.')
            }
          }}
        />
      )}

      <ConfirmDialog
        open={!!confirmDel}
        title="Delete virtual key?"
        body={
          confirmDel
            ? `Delete "${confirmDel.name}"? Its budget and rate limits go with it.${
                confirmDel.value && confirmDel.value === activeVk
                  ? ' Vigil is currently sending this key, so LLM calls will run unmetered until another is set.'
                  : ''
              }`
            : ''
        }
        confirmLabel="Delete"
        busy={busy}
        onConfirm={handleDelete}
        onClose={() => setConfirmDel(null)}
      />
    </>
  )
}

/* ---------------- Virtual key editor ---------------- */
function VirtualKeyDialog({
  existing,
  onClose,
  onSave,
}: {
  existing: BifrostVirtualKey | null
  onClose: () => void
  onSave: (data: BifrostVirtualKeyWrite) => Promise<void>
}) {
  const [name, setName] = useState(existing?.name || '')
  const [description, setDescription] = useState(existing?.description || '')
  const [isActive, setIsActive] = useState(existing?.is_active ?? true)
  const [capped, setCapped] = useState(!!existing?.budget?.max_limit)
  const [maxLimit, setMaxLimit] = useState(existing?.budget?.max_limit ?? 100)
  const [resetDuration, setResetDuration] = useState(existing?.budget?.reset_duration || 'monthly')
  const [throttled, setThrottled] = useState(!!existing?.rate_limit?.request_max_limit)
  const [requestLimit, setRequestLimit] = useState(existing?.rate_limit?.request_max_limit ?? 60)
  const [tokenLimit, setTokenLimit] = useState(existing?.rate_limit?.token_max_limit ?? 0)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    setSaving(true)
    setErr(null)
    try {
      await onSave({
        name: name.trim(),
        description: description.trim() || undefined,
        is_active: isActive,
        budget: capped ? { max_limit: Number(maxLimit) || 0, reset_duration: resetDuration } : null,
        rate_limit: throttled
          ? {
              request_max_limit: Number(requestLimit) || 0,
              request_reset_duration: 'minute',
              ...(Number(tokenLimit) > 0
                ? { token_max_limit: Number(tokenLimit), token_reset_duration: 'minute' }
                : {}),
            }
          : null,
      })
    } catch (e) {
      setErr(bifrostError(e, 'Save failed.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Popup open onClose={onClose} title={existing ? `Edit ${existing.name}` : 'New virtual key'}>
      {err && <div className="settings-banner err mb-3"><Icon name="alert" size={14} /> {err}</div>}
      <div className="flex flex-col gap-3.5">
        <Field label="Name" hint="How this key appears in the gateway's logs and cost breakdowns.">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="vigil-soc" />
        </Field>
        <Field label="Description">
          <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label="Enabled" hint="A disabled key is refused by the gateway.">
          <Toggle checked={isActive} onChange={setIsActive} />
        </Field>

        <div style={{ paddingTop: 12, borderTop: '1px solid var(--line)' }}>
          <Field label="Spend ceiling" hint="Off means unmetered — the gateway will not stop a runaway loop.">
            <Toggle checked={capped} onChange={setCapped} />
          </Field>
          {capped && (
            <div className="settings-grid-2 mt-3" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)' }}>
              <Field label="Ceiling (USD)">
                <NumberInput value={maxLimit} min={0} onChange={(e) => setMaxLimit(Number(e.target.value))} />
              </Field>
              <Field label="Resets">
                <Select value={resetDuration} options={RESET_OPTIONS} onSelect={setResetDuration} />
              </Field>
            </div>
          )}
        </div>

        <div style={{ paddingTop: 12, borderTop: '1px solid var(--line)' }}>
          <Field label="Rate limit" hint="Caps requests per minute against this key.">
            <Toggle checked={throttled} onChange={setThrottled} />
          </Field>
          {throttled && (
            <div className="settings-grid-2 mt-3" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)' }}>
              <Field label="Requests / minute">
                <NumberInput value={requestLimit} min={0} onChange={(e) => setRequestLimit(Number(e.target.value))} />
              </Field>
              <Field label="Tokens / minute" hint="0 for no token cap.">
                <NumberInput value={tokenLimit} min={0} onChange={(e) => setTokenLimit(Number(e.target.value))} />
              </Field>
            </div>
          )}
        </div>
      </div>
      <div className="flex justify-end gap-2.5 mt-5">
        <button className="btn ghost" onClick={onClose} disabled={saving}>Cancel</button>
        <button className="btn primary" disabled={saving || !name.trim()} onClick={submit}>
          <Icon name="check2" /> {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </Popup>
  )
}
