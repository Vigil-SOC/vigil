// The cost-guardrails step reads ready once default_vk is non-empty.
// budget_limit_usd and enforcement_mode are stored and ignored; round-trip
// them so a save does not wipe the blob.
import { useEffect, useState } from 'react'
import { Field, TextInput } from '../../shared/ui'
import { Banner, StepFooter, useSaveAction } from '../../shared/formKit'
import { budgetsApi, type BudgetSettings } from '../../services/api'

interface Props {
  onClose: () => void
  onSaved: () => void
}

const BudgetDialog = ({ onClose, onSaved }: Props) => {
  const [vk, setVk] = useState('')
  const [stored, setStored] = useState<Pick<BudgetSettings, 'budget_limit_usd' | 'enforcement_mode'>>({
    budget_limit_usd: 0,
    enforcement_mode: 'warning',
  })
  const [vkError, setVkError] = useState<string | null>(null)
  const { saving, error, run } = useSaveAction({ onSaved })

  useEffect(() => {
    let alive = true
    budgetsApi
      .get()
      .then(({ data }) => {
        if (!alive || !data) return
        setVk(data.default_vk ?? '')
        setStored({
          budget_limit_usd: data.budget_limit_usd ?? 0,
          enforcement_mode: data.enforcement_mode ?? 'warning',
        })
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  const save = () => {
    if (!vk.trim()) {
      setVkError('Add a Bifrost virtual key — Vigil bills every call against it.')
      return
    }
    run(async () => {
      await budgetsApi.set({
        default_vk: vk.trim(),
        budget_limit_usd: stored.budget_limit_usd,
        enforcement_mode: stored.enforcement_mode,
      })
    }, 'Failed to save budget')
  }

  return (
    <div className="flex flex-col gap-3.5">
      {error && <Banner kind="err">{error}</Banner>}
      <p className="text-sm text-tx-2">
        Point Vigil at a Bifrost virtual key. The spend ceiling is that key&apos;s own
        budget, set under Settings → AI Config → Virtual Keys.
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
