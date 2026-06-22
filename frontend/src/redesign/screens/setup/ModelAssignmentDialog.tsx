// frontend/src/redesign/screens/setup/ModelAssignmentDialog.tsx
//
// Onboarding step panel — assigns the default chat model (chat_default), which
// every unset component inherits. Satisfies the "model assignment" checklist
// step (ready === at least one assignment). Renders inline on the setup screen
// (no modal). Per-agent tuning stays in Settings → AI Config. Direct aiConfigApi
// calls; no redesign settings-section dependency.
import { useEffect, useState } from 'react'
import { Field, Select } from '../../shared/ui'
import { aiConfigApi, type AIModelInfo } from '../../../services/api'

interface Props {
  onClose: () => void
  onSaved: () => void
  onError: (msg: string) => void
}

// provider_id + model_id, joined for the Select value. Ollama model ids contain
// single colons (llama3.1:8b), so we split on the first "::" only.
const SEP = '::'

const ModelAssignmentDialog = ({ onClose, onSaved, onError }: Props) => {
  const [models, setModels] = useState<AIModelInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let alive = true
    aiConfigApi
      .listModels()
      .then(({ data }) => alive && setModels(data.models || []))
      .catch(() => alive && setModels([]))
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  const save = async () => {
    const i = selected.indexOf(SEP)
    if (i < 0) return
    setSaving(true)
    try {
      await aiConfigApi.setComponent('chat_default', {
        provider_id: selected.slice(0, i),
        model_id: selected.slice(i + SEP.length),
      })
      onSaved()
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string }
      onError(err?.response?.data?.detail || err?.message || 'Failed to assign model')
      setSaving(false)
    }
  }

  const options = models.map((m) => ({
    value: `${m.provider_id}${SEP}${m.model_id}`,
    label: `${m.display_name || m.model_id} · ${m.provider_type}`,
  }))

  return (
    <div className="flex flex-col gap-3.5">
      <p className="text-sm text-tx-2">
        Pick the default model for chat and any agent without its own assignment. You can set
        per-agent models — cheap for triage, strong for investigation — later in Settings → AI
        Config.
      </p>
      <Field
        label="Default model"
        hint={loading ? 'Loading available models…' : `${models.length} model(s) available.`}
      >
        <Select
          value={selected}
          placeholder={loading ? 'Loading…' : 'Select a model'}
          options={options}
          onSelect={setSelected}
        />
      </Field>
      <div className="flex justify-end gap-2.5 mt-2">
        <button className="btn ghost" onClick={onClose} disabled={saving}>
          Cancel
        </button>
        <button className="btn primary" onClick={save} disabled={saving || !selected}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  )
}

export default ModelAssignmentDialog
