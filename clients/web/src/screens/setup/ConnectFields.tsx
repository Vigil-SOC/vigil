import { useMemo, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Field, NumberInput, PasswordInput, Select, TextInput, ToggleRow } from '../../shared/ui'
import {
  SECTION_LABELS,
  type IntegrationField,
  type IntegrationMetadata,
} from '../../config/integrationSchema'
import { fieldsOf, type ConnectConfig } from './connectConfig'

/** The integration's form fields: main ones three to a row, sectioned ones under "More settings". */
export default function ConnectFields({
  integration,
  config,
  secretsSet,
  onChange,
}: {
  integration: IntegrationMetadata
  config: ConnectConfig
  secretsSet: Record<string, boolean>
  onChange: (name: string, value: unknown) => void
}) {
  const [open, setOpen] = useState(false)
  const fields = useMemo(() => fieldsOf(integration), [integration])
  const main = fields.filter((f) => !f.section)
  const sectioned = fields.filter((f) => f.section)
  const sections = useMemo(() => {
    const groups: Record<string, IntegrationField[]> = {}
    for (const f of fields) if (f.section) (groups[f.section] ??= []).push(f)
    return groups
  }, [fields])

  const render = (f: IntegrationField) => {
    const value = config[f.name] ?? f.default ?? ''
    if (f.type === 'boolean')
      return (
        <ToggleRow
          key={f.name}
          label={f.label}
          hint={f.helpText}
          checked={Boolean(value)}
          onChange={(v) => onChange(f.name, v)}
        />
      )
    if (f.type === 'select')
      return (
        <Field key={f.name} label={f.label} hint={f.helpText}>
          <Select value={String(value)} options={f.options || []} onSelect={(v) => onChange(f.name, v)} />
        </Field>
      )
    if (f.type === 'number')
      return (
        <Field key={f.name} label={f.label} hint={f.helpText}>
          <NumberInput
            value={value as number}
            placeholder={f.placeholder}
            onChange={(e) => onChange(f.name, parseInt(e.target.value, 10) || 0)}
          />
        </Field>
      )
    if (f.type === 'password')
      return (
        <Field key={f.name} label={f.label} hint={f.helpText}>
          <PasswordInput
            value={String(value)}
            placeholder={secretsSet[f.name] === true ? '•••••••• saved — leave blank to keep' : f.placeholder}
            onChange={(e) => onChange(f.name, e.target.value)}
          />
        </Field>
      )
    return (
      <Field key={f.name} label={f.label} hint={f.helpText}>
        <TextInput value={String(value)} placeholder={f.placeholder} onChange={(e) => onChange(f.name, e.target.value)} />
      </Field>
    )
  }

  return (
    <>
      {main.length > 0 && <div className="su-fields">{main.map(render)}</div>}
      {sectioned.length > 0 && (
        <div className="su-more-settings">
          <button
            type="button"
            className="su-toggle"
            aria-expanded={open}
            onClick={() => setOpen((o) => !o)}
          >
            <Icon name={open ? 'chevD' : 'chevR'} size={14} />
            More settings
          </button>
          {open &&
            Object.entries(sections).map(([name, group]) => (
              <div key={name} className="flex flex-col gap-3">
                <span className="text-xs font-semibold text-tx-2">{SECTION_LABELS[name] || name}</span>
                <div className="su-fields">{group.map(render)}</div>
              </div>
            ))}
        </div>
      )}
    </>
  )
}
