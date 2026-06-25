// frontend/src/redesign/screens/setup/ModelAssignmentDialog.tsx
//
// Setup step panel — assigns the default chat model (chat_default), which every
// unset component inherits. Satisfies the model-assignment step (≥1 assignment).
import { useEffect, useState } from 'react'
import { Field, Select } from '../../shared/ui'
import { Banner, StepFooter, useSaveAction } from '../../shared/formKit'
import { aiConfigApi, type AIModelInfo } from '../../../services/api'

interface Props {
  onClose: () => void
  onSaved: () => void
}

// provider_id + model_id, joined for the Select value. Ollama model ids contain
// single colons (llama3.1:8b), so we split on the first "::" only.
const SEP = '::'

const ModelAssignmentDialog = ({ onClose, onSaved }: Props) => {
  const [models, setModels] = useState<AIModelInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState('')
  const [selectError, setSelectError] = useState<string | null>(null)
  const { saving, error, run } = useSaveAction({ onSaved })

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

  const save = () => {
    // Validate on click instead of disabling Save with no explanation.
    if (!selected) {
      setSelectError('Pick a model to assign.')
      return
    }
    const i = selected.indexOf(SEP)
    if (i < 0) return
    run(async () => {
      await aiConfigApi.setComponent('chat_default', {
        provider_id: selected.slice(0, i),
        model_id: selected.slice(i + SEP.length),
      })
    }, 'Failed to assign model')
  }

  const options = models.map((m) => ({
    value: `${m.provider_id}${SEP}${m.model_id}`,
    label: `${m.display_name || m.model_id} · ${m.provider_type}`,
  }))

  return (
    <div className="flex flex-col gap-3.5">
      {error && <Banner kind="err">{error}</Banner>}
      <p className="text-sm text-tx-2">
        Pick the default model for chat and any agent without its own assignment. You can set
        per-agent models — cheap for triage, strong for investigation — later in Settings → AI
        Config.
      </p>
      <Field
        label="Default model"
        hint={loading ? 'Loading available models…' : `${models.length} model(s) available.`}
        error={selectError}
      >
        <Select
          value={selected}
          placeholder={loading ? 'Loading…' : 'Select a model'}
          options={options}
          onSelect={(v) => {
            setSelected(v)
            if (selectError) setSelectError(null)
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

export default ModelAssignmentDialog
