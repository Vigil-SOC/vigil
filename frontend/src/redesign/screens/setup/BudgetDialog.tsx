// frontend/src/redesign/screens/setup/BudgetDialog.tsx
//
// Onboarding step panel — sets the Bifrost virtual key + spend cap that the
// "cost guardrails" checklist step keys off (ready === non-empty default_vk).
// Renders inline on the setup screen (no modal). Direct budgetsApi calls; no
// redesign settings-section dependency.
import { useEffect, useState } from 'react'
import { Field, NumberInput, Select, TextInput } from '../../shared/ui'
import { budgetsApi, type BudgetSettings } from '../../../services/api'

interface Props {
  onClose: () => void
  onSaved: () => void
  onError: (msg: string) => void
}

const ENFORCEMENT_OPTIONS = [
  { value: 'warning', label: 'Warn only — log overages, keep running' },
  { value: 'hard_stop', label: 'Hard stop — block calls once the cap is hit' },
]

const BudgetDialog = ({ onClose, onSaved, onError }: Props) => {
  const [vk, setVk] = useState('')
  const [limit, setLimit] = useState('')
  const [enforcement, setEnforcement] = useState<BudgetSettings['enforcement_mode']>('warning')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let alive = true
    budgetsApi
      .get()
      .then(({ data }) => {
        if (!alive || !data) return
        setVk(data.default_vk ?? '')
        if (data.budget_limit_usd) setLimit(String(data.budget_limit_usd))
        if (data.enforcement_mode) setEnforcement(data.enforcement_mode)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  const save = async () => {
    setSaving(true)
    try {
      await budgetsApi.set({
        default_vk: vk.trim(),
        budget_limit_usd: Number(limit) || 0,
        enforcement_mode: enforcement,
      })
      onSaved()
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      onError(err?.response?.data?.detail || err?.message || 'Failed to save budget')
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-3.5">
      <p className="text-sm text-tx-2">
        Cap spend through a Bifrost virtual key. Vigil reads the key&apos;s live usage and
        enforces the limit on every model call.
      </p>
      <Field
        label="Bifrost virtual key"
        hint="The virtual key Vigil bills against — copy it from your Bifrost dashboard."
      >
        <TextInput value={vk} placeholder="vk-…" onChange={(e) => setVk(e.target.value)} />
      </Field>
      <Field label="Monthly spend cap (USD)">
        <NumberInput
          min={0}
          value={limit}
          placeholder="e.g. 500"
          onChange={(e) => setLimit(e.target.value)}
        />
      </Field>
      <Field label="Enforcement">
        <Select
          value={enforcement}
          options={ENFORCEMENT_OPTIONS}
          onSelect={(v) => setEnforcement(v as BudgetSettings['enforcement_mode'])}
        />
      </Field>
      <div className="flex justify-end gap-2.5 mt-2">
        <button className="btn ghost" onClick={onClose} disabled={saving}>
          Cancel
        </button>
        <button className="btn primary" onClick={save} disabled={saving || !vk.trim()}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  )
}

export default BudgetDialog
