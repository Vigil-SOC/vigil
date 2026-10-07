import { useCallback, useEffect, useState } from 'react'
import { budgetsApi } from '../../services/api'
import { bifrostApi, type BifrostBudget, type BifrostVirtualKey } from '../../services/bifrostApi'
import { InfoTip } from '../../shared/InfoTip'
import { NotMeasured } from '../../shared/NotMeasured'
import { ConfirmDialog, Field, NumberInput } from '../../shared/ui'
import { bifrostError } from '../settings/useBifrost'
import { MONTHLY, periodBesideCeiling } from './ceilingPeriod'

const budgetOf = (key: BifrostVirtualKey): BifrostBudget | undefined => key.budgets?.[0] ?? key.budget ?? undefined

// no ceiling (0) is the highest one there is
const level = (n: number) => (n > 0 ? n : Infinity)

type Load =
  | { kind: 'loading' }
  | { kind: 'budget-error' }
  | { kind: 'list-error' }
  | { kind: 'later' }
  | { kind: 'key'; key: BifrostVirtualKey }

/** The default virtual key's budget, found by the name the quota endpoint reports (the list's values are masked). */
async function loadCeiling(): Promise<Load> {
  let name: string | undefined
  try {
    const { data } = await budgetsApi.getQuota()
    if (!data?.configured || !data.available) return { kind: 'later' }
    name = data.quota?.virtual_key_name
  } catch {
    return { kind: 'budget-error' }
  }
  if (!name) return { kind: 'later' }
  try {
    const { data } = await bifrostApi.listVirtualKeys()
    const matches = (data.virtual_keys ?? []).filter((key) => key.name === name)
    return matches.length === 1 ? { kind: 'key', key: matches[0] } : { kind: 'later' }
  } catch {
    return { kind: 'list-error' }
  }
}

const parseCeiling = (draft: string): number | null => {
  if (draft.trim() === '') return 0
  const n = Number(draft)
  return Number.isFinite(n) && n >= 0 ? n : null
}

export default function MonthlyCeiling() {
  const [load, setLoad] = useState<Load>({ kind: 'loading' })
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoad({ kind: 'loading' })
    const next = await loadCeiling()
    if (next.kind === 'key') setDraft(String(budgetOf(next.key)?.max_limit || ''))
    setLoad(next)
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  const key = load.kind === 'key' ? load.key : null
  const current = (key && budgetOf(key)?.max_limit) || 0
  const next = parseCeiling(draft)

  const save = async (value: number) => {
    if (!key) return
    setError(null)
    setSaving(true)
    try {
      // a PUT with only the budget leaves the key's name, rate limit and allow-lists alone
      const reset = budgetOf(key)?.reset_duration || MONTHLY
      await bifrostApi.updateVirtualKey(key.id, {
        name: key.name,
        budgets: value > 0 ? [{ max_limit: value, reset_duration: reset }] : [],
      })
      // show what the gateway holds
      const { data } = await bifrostApi.listVirtualKeys()
      const fresh = (data.virtual_keys ?? []).find((k) => k.id === key.id)
      if (!fresh) throw new Error('The key is no longer on the gateway.')
      setLoad({ kind: 'key', key: fresh })
      setDraft(String(budgetOf(fresh)?.max_limit || ''))
    } catch (err) {
      setError(bifrostError(err, 'Could not save the monthly ceiling.'))
    } finally {
      setSaving(false)
    }
  }

  const submit = () => {
    if (next === null) return
    if (level(next) > level(current)) setPending(next)
    else save(next)
  }

  const period = key ? periodBesideCeiling(budgetOf(key)?.reset_duration) : null
  const dirty = key !== null && next !== current
  const retry = (
    <button type="button" className="su-more" onClick={reload}>
      Retry
    </button>
  )

  return (
    <div className="su-ceiling">
      <div className="su-ceiling-field">
        {load.kind === 'later' ? (
          <div className="field">
            <span className="field-label">Monthly ceiling</span>
            <span className="su-later">
              Later
              <InfoTip
                label="Why the monthly ceiling is not available"
                text="No default virtual key is set. Add one in Settings › AI models."
                align="start"
              />
            </span>
          </div>
        ) : (
          <Field
            label="Monthly ceiling"
            hint="The model gateway checks it before every call."
            error={next === null ? 'Enter an amount in dollars, or leave it empty for no ceiling.' : null}
          >
            <span className="su-ceiling-row">
              <span className="su-money">
                <span aria-hidden="true">$</span>
                <NumberInput
                  aria-label="Monthly ceiling"
                  min={0}
                  value={draft}
                  placeholder="No ceiling"
                  disabled={!key || saving}
                  onChange={(e) => setDraft(e.target.value)}
                />
              </span>
              {period && <span className="su-note">Resets {period}</span>}
              {dirty && next !== null && (
                <button type="button" className="btn" disabled={saving} onClick={submit}>
                  {saving ? 'Saving…' : 'Save ceiling'}
                </button>
              )}
            </span>
          </Field>
        )}
        {load.kind === 'loading' && <p className="su-note">Reading the gateway budget…</p>}
        {load.kind === 'budget-error' && (
          <p className="su-note err" role="alert">
            Could not read the gateway budget. {retry}
          </p>
        )}
        {load.kind === 'list-error' && (
          <p className="su-note err" role="alert">
            Could not read the gateway’s virtual keys. {retry}
          </p>
        )}
        {error && (
          <p className="su-note err" role="alert">
            {error}
          </p>
        )}
      </div>
      <div className="su-estimate">
        <NotMeasured label="Estimated monthly spend" tip="Nothing projects monthly spend yet." />
      </div>

      <ConfirmDialog
        open={pending !== null}
        title="Raise the monthly ceiling?"
        body="This lets the gateway spend more each month, or removes the ceiling. Confirm to save."
        confirmLabel="Save"
        danger={false}
        onConfirm={() => {
          const value = pending
          setPending(null)
          if (value !== null) save(value)
        }}
        onClose={() => setPending(null)}
      />
    </div>
  )
}
