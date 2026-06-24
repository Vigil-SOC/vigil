// frontend/src/redesign/screens/setup/BudgetDialog.tsx
//
// Setup step panel — sets the Bifrost virtual key + spend cap. The cost-guardrails
// step reads ready once default_vk is non-empty.
import { useEffect, useState } from 'react'
import { Field, NumberInput, Select, TextInput } from '../../shared/ui'
import { Banner, StepFooter, useSaveAction } from '../../shared/formKit'
import { budgetsApi, type BudgetSettings } from '../../../services/api'

interface Props {
  onClose: () => void
  onSaved: () => void
}

const ENFORCEMENT_OPTIONS = [
  { value: 'warning', label: 'Warn only — log overages, keep running' },
  { value: 'hard_stop', label: 'Hard stop — block calls once the cap is hit' },
]

const BudgetDialog = ({ onClose, onSaved }: Props) => {
  const [vk, setVk] = useState('')
  const [limit, setLimit] = useState('')
  const [enforcement, setEnforcement] = useState<BudgetSettings['enforcement_mode']>('warning')
  const [vkError, setVkError] = useState<string | null>(null)
  const { saving, error, run } = useSaveAction({ onSaved })

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

  const save = () => {
    // vk is the one required field (the cap is enforced through it). Validate on
    // click instead of disabling Save with no explanation.
    if (!vk.trim()) {
      setVkError('Add a Bifrost virtual key — Vigil enforces the spend cap through it.')
      return
    }
    run(async () => {
      await budgetsApi.set({
        default_vk: vk.trim(),
        budget_limit_usd: Number(limit) || 0,
        enforcement_mode: enforcement,
      })
    }, 'Failed to save budget')
  }

  return (
    <div className="flex flex-col gap-3.5">
      {error && <Banner kind="err">{error}</Banner>}
      <p className="text-sm text-tx-2">
        Cap spend through a Bifrost virtual key. Vigil reads the key&apos;s live usage and
        enforces the limit on every model call.
      </p>
      <Field
        label="Bifrost virtual key"
        hint="The virtual key Vigil bills against — copy it from your Bifrost dashboard."
        error={vkError}
      >
        <TextInput
          value={vk}
          placeholder="vk-…"
          onChange={(e) => {
            setVk(e.target.value)
            if (vkError) setVkError(null)
          }}
        />
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
      <StepFooter
        onCancel={onClose}
        saving={saving}
        onPrimary={save}
        primaryLabel="Save"
        busyLabel="Saving…"
      />
    </div>
  )
}

export default BudgetDialog
