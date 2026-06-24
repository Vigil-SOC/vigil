/* ============================================================
   Per-integration credential wizard (redesign port). Renders the
   dynamic field set from IntegrationMetadata (text/password/url/
   number/boolean/select) plus the optional collapsible proxy block,
   and saves via configApi.setIntegrations (merge handled by the hook).
   Mirrors components/settings/IntegrationWizard.tsx.
   ============================================================ */
import { useMemo, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Field, NumberInput, PasswordInput, Popup, Select, TextInput, ToggleRow } from '../../shared/ui'
import { Banner, extractApiError } from '../../shared/formKit'
import {
  PROXY_FIELDS,
  SECTION_LABELS,
  type IntegrationField,
  type IntegrationMetadata,
} from '../../../components/settings/IntegrationWizard'

interface Props {
  integration: IntegrationMetadata
  existingConfig?: Record<string, unknown>
  onClose: () => void
  onSave: (id: string, config: Record<string, unknown>) => Promise<void>
}

export default function IntegrationWizard({ integration, existingConfig = {}, onClose, onSave }: Props) {
  const [config, setConfig] = useState<Record<string, unknown>>(existingConfig)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [proxyOpen, setProxyOpen] = useState(false)

  const fields = useMemo(
    () => (integration.proxy_supported ? [...integration.fields, ...PROXY_FIELDS] : integration.fields),
    [integration],
  )
  const mainFields = fields.filter((f) => !f.section)
  const sections = useMemo(() => {
    const groups: Record<string, IntegrationField[]> = {}
    for (const f of fields) {
      if (!f.section) continue
      const key = f.section
      if (!groups[key]) groups[key] = []
      groups[key].push(f)
    }
    return groups
  }, [fields])

  const set = (name: string, val: unknown) => setConfig((c) => ({ ...c, [name]: val }))

  const renderField = (f: IntegrationField) => {
    const value = config[f.name] ?? f.default ?? ''
    if (f.type === 'boolean') {
      return (
        <ToggleRow key={f.name} label={f.label} hint={f.helpText} checked={Boolean(value)} onChange={(v) => set(f.name, v)} />
      )
    }
    if (f.type === 'select') {
      return (
        <Field key={f.name} label={f.label} hint={f.helpText}>
          <Select value={String(value)} options={f.options || []} onSelect={(v) => set(f.name, v)} />
        </Field>
      )
    }
    if (f.type === 'number') {
      return (
        <Field key={f.name} label={f.label} hint={f.helpText}>
          <NumberInput value={value as number} placeholder={f.placeholder} onChange={(e) => set(f.name, parseInt(e.target.value, 10) || 0)} />
        </Field>
      )
    }
    if (f.type === 'password') {
      return (
        <Field key={f.name} label={f.label} hint={f.helpText}>
          <PasswordInput value={String(value)} placeholder={f.placeholder} onChange={(e) => set(f.name, e.target.value)} />
        </Field>
      )
    }
    return (
      <Field key={f.name} label={f.label} hint={f.helpText}>
        <TextInput value={String(value)} placeholder={f.placeholder} onChange={(e) => set(f.name, e.target.value)} />
      </Field>
    )
  }

  const requiredOk = fields
    .filter((f) => f.required)
    .every((f) => {
      const v = config[f.name] ?? f.default
      return v !== undefined && v !== ''
    })

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      await onSave(integration.id, config)
      onClose()
    } catch (e) {
      setError(extractApiError(e, 'Failed to save configuration'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Popup open onClose={onClose} title={`Configure ${integration.name}`} width={520}>
      <div className="flex flex-col gap-3.5">
        <p className="text-sm text-tx-3 leading-relaxed">{integration.description}</p>
        {integration.docs_url && (
          <a className="text-xs text-accent-2 inline-flex items-center gap-1 -mt-1" href={integration.docs_url} target="_blank" rel="noreferrer">
            <Icon name="link" size={12} /> Documentation
          </a>
        )}
        {error && <Banner kind="err">{error}</Banner>}

        {mainFields.map(renderField)}

        {Object.entries(sections).map(([name, secFields]) => (
          <div key={name} className="card card-sq">
            <button
              className="card-h w-full text-left"
              style={{ cursor: 'pointer' }}
              onClick={() => setProxyOpen((o) => !o)}
            >
              <h3 className="flex-1">{SECTION_LABELS[name] || name}</h3>
              <Icon name={proxyOpen ? 'chevD' : 'chevR'} size={15} />
            </button>
            {proxyOpen && <div className="card-b flex flex-col gap-3.5">{secFields.map(renderField)}</div>}
          </div>
        ))}

        <div className="flex justify-end gap-2.5 mt-1">
          <button className="btn ghost" onClick={onClose} disabled={saving}>Cancel</button>
          <button className="btn primary" onClick={handleSave} disabled={!requiredOk || saving}>
            <Icon name="check2" /> {saving ? 'Saving…' : 'Save Configuration'}
          </button>
        </div>
      </div>
    </Popup>
  )
}
