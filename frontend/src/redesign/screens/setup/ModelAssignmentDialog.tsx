// frontend/src/redesign/screens/setup/ModelAssignmentDialog.tsx
//
// Setup step panel — assigns models per component, mirroring Settings → AI Config.
// chat_default is the required base every unset component inherits; the rest are
// optional overrides. Any assignment satisfies the model-assignment step.
import { useEffect, useState } from 'react'
import { Field, Select } from '../../shared/ui'
import { Banner, StepFooter, useSaveAction } from '../../shared/formKit'
import { aiConfigApi, type AIModelInfo } from '../../../services/api'
import { COMPONENT_LABELS, CHAT_DEFAULT_KEY } from '../../../config/aiComponents'

interface Props {
  onClose: () => void
  onSaved: () => void
}

// provider_id + model_id, joined for the Select value. Ollama model ids contain
// single colons (llama3.1:8b), so we split on the first "::" only.
const SEP = '::'
// Sentinel for "no override, inherit chat_default". Real values contain "::".
const INHERIT = 'inherit'

const ModelAssignmentDialog = ({ onClose, onSaved }: Props) => {
  const [models, setModels] = useState<AIModelInfo[]>([])
  const [components, setComponents] = useState<string[]>([])
  // component id -> selected value ("provider::model" or INHERIT). `initial` is the
  // loaded snapshot so save only writes the rows the user actually changed.
  const [rows, setRows] = useState<Record<string, string>>({})
  const [initial, setInitial] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [selectError, setSelectError] = useState<string | null>(null)
  const { saving, error, run } = useSaveAction({ onSaved })

  useEffect(() => {
    let alive = true
    // Fail-open (allSettled): a flaky /ai/config still lets the user set the default.
    Promise.allSettled([aiConfigApi.listModels(), aiConfigApi.getConfig()]).then(
      ([modelsRes, cfgRes]) => {
        if (!alive) return
        setModels(modelsRes.status === 'fulfilled' ? modelsRes.value.data.models || [] : [])
        const cfg = cfgRes.status === 'fulfilled' ? cfgRes.value.data : null
        const raw = cfg?.components?.length ? cfg.components : Object.keys(COMPONENT_LABELS)
        // chat_default is rendered and validated unconditionally, so keep it in
        // the list the save loop iterates — else a backend list missing it would
        // validate but never persist the required default.
        const comps = raw.includes(CHAT_DEFAULT_KEY) ? raw : [CHAT_DEFAULT_KEY, ...raw]
        setComponents(comps)
        const next: Record<string, string> = {}
        for (const c of comps) {
          const a = cfg?.assignments?.[c]
          next[c] = a
            ? `${a.provider_id}${SEP}${a.model_id}`
            : c === CHAT_DEFAULT_KEY
              ? ''
              : INHERIT
        }
        setRows(next)
        setInitial(next)
      },
    ).finally(() => {
      if (alive) setLoading(false)
    })
    return () => {
      alive = false
    }
  }, [])

  const modelOptions = models.map((m) => ({
    value: `${m.provider_id}${SEP}${m.model_id}`,
    label: `${m.display_name || m.model_id} · ${m.provider_type}`,
  }))
  const optionsFor = (component: string) =>
    component === CHAT_DEFAULT_KEY
      ? modelOptions
      : [{ value: INHERIT, label: 'Inherit default' }, ...modelOptions]

  const setRow = (component: string, value: string) => {
    setRows((prev) => ({ ...prev, [component]: value }))
    if (component === CHAT_DEFAULT_KEY && selectError) setSelectError(null)
  }

  const parse = (value: string) => {
    const i = value.indexOf(SEP)
    return { provider_id: value.slice(0, i), model_id: value.slice(i + SEP.length) }
  }

  const save = () => {
    // Validate on click instead of disabling Save with no explanation. chat_default
    // is required — it's the base every unset component falls back to.
    if (!(rows[CHAT_DEFAULT_KEY] || '').includes(SEP)) {
      setSelectError(
        models.length === 0 ? 'Connect an AI provider first.' : 'Pick a default model.',
      )
      return
    }
    run(async () => {
      const ops: Promise<unknown>[] = []
      for (const c of components) {
        const desired = rows[c] ?? (c === CHAT_DEFAULT_KEY ? '' : INHERIT)
        const was = initial[c] ?? (c === CHAT_DEFAULT_KEY ? '' : INHERIT)
        if (desired === was) continue
        if (desired === INHERIT) {
          // switched back to inherit — clear only if a stored assignment existed
          if (was !== INHERIT) ops.push(aiConfigApi.clearComponent(c))
        } else {
          ops.push(aiConfigApi.setComponent(c, parse(desired)))
        }
      }
      await Promise.all(ops)
    }, 'Failed to save model assignments')
  }

  const renderRow = (component: string, required: boolean) => {
    const meta = COMPONENT_LABELS[component] ?? { label: component, description: '' }
    return (
      <Field
        key={component}
        label={meta.label}
        hint={required && loading ? 'Loading available models…' : meta.description}
        error={required ? selectError : null}
      >
        <Select
          value={rows[component] ?? (required ? '' : INHERIT)}
          placeholder={loading ? 'Loading…' : required ? 'Select a model' : 'Inherit default'}
          options={optionsFor(component)}
          onSelect={(v) => setRow(component, v)}
        />
      </Field>
    )
  }

  return (
    <div className="flex flex-col gap-3.5">
      {error && <Banner kind="err">{error}</Banner>}
      <p className="text-sm text-tx-2">
        Pick the default model for chat and any agent without its own assignment, then
        optionally override it per agent — cheap for triage, strong for investigation.
      </p>
      {!loading && models.length === 0 && (
        <p className="text-sm text-tx-3">
          No models available yet — finish connecting an AI provider first.
        </p>
      )}
      {renderRow(CHAT_DEFAULT_KEY, true)}
      <div className="text-tx-3 text-xs font-medium uppercase tracking-wide mt-1">
        Per-agent overrides (optional)
      </div>
      {components.filter((c) => c !== CHAT_DEFAULT_KEY).map((c) => renderRow(c, false))}
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
