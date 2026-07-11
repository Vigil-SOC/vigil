// frontend/src/redesign/shared/formKit.tsx
//
// Shared helpers for the inline dialog/step forms (setup steps + settings wizards):
// error normalization, the save lifecycle, the Cancel+primary footer, the banner.
import { useState, type ReactNode } from 'react'
import { Icon } from './icons'

export const extractApiError = (e: unknown, fallback: string): string => {
  const err = e as { response?: { data?: { detail?: string } }; message?: string }
  return err?.response?.data?.detail || err?.message || fallback
}

export const Banner = ({ kind, children }: { kind: 'err' | 'ok'; children: ReactNode }) => (
  <div className={`settings-banner ${kind}`}>
    <Icon name={kind === 'err' ? 'alert' : 'check2'} size={14} /> {children}
  </div>
)

// `saving` deliberately stays true on success so the panel can unmount on refetch
// without flashing the button back; it resets only on error so the user can retry.
export const useSaveAction = ({ onSaved }: { onSaved: () => void }) => {
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async (task: () => Promise<void>, fallback: string) => {
    setSaving(true)
    setError(null)
    try {
      await task()
      onSaved()
    } catch (e) {
      setError(extractApiError(e, fallback))
      setSaving(false)
    }
  }
  return { saving, error, run }
}

export const StepFooter = ({
  onCancel,
  saving,
  onPrimary,
  primaryLabel,
  busyLabel,
  primaryDisabled,
}: {
  onCancel: () => void
  saving: boolean
  onPrimary: () => void
  primaryLabel: string
  busyLabel: string
  primaryDisabled?: boolean
}) => (
  <div className="flex justify-end gap-2.5 mt-2">
    <button className="btn ghost" onClick={onCancel} disabled={saving}>
      Cancel
    </button>
    <button className="btn primary" onClick={onPrimary} disabled={saving || primaryDisabled}>
      {saving ? busyLabel : primaryLabel}
    </button>
  </div>
)
