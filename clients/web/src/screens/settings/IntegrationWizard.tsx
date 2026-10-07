import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { LevelBadge, type Level } from '../../shared/LevelBadge'
import { configApi, type IntegrationTestResult } from '../../services/api'
import { Field, NumberInput, PasswordInput, Popup, Select, TextInput, ToggleRow } from '../../shared/ui'
import { Banner, extractApiError } from '../../shared/formKit'
import { relativeTime, type LastTest } from './integrationHealth'
import {
  PROXY_FIELDS,
  SECTION_LABELS,
  type IntegrationField,
  type IntegrationMetadata,
} from '../../config/integrationSchema'

interface Props {
  integration: IntegrationMetadata
  existingConfig?: Record<string, unknown>
  // Per-field {secretField: isSet} from the backend — booleans only, no values.
  secretsSet?: Record<string, boolean>
  onClose: () => void
  onSave: (id: string, config: Record<string, unknown>) => Promise<void>
  // 'drawer' is Settings' setup drawer; 'setup' is the first-run popup: fields only.
  variant?: 'drawer' | 'setup'
  // drawer header: the stored last test, and the category line under the name
  lastTest?: LastTest
  category?: string
  // a test ran in the drawer: the parent refreshes its health read
  onTested?: () => void
}

const STEPS = ['Add connection', 'Verify a read', 'Collect alerts', 'Automatic investigations'] as const

type Probe =
  | { state: 'idle' }
  | { state: 'running' }
  | { state: 'done'; result: IntegrationTestResult }
  | { state: 'error'; message: string; notConfigured?: boolean }

export default function IntegrationWizard({
  integration,
  existingConfig = {},
  secretsSet = {},
  onClose,
  onSave,
  variant = 'drawer',
  lastTest,
  category,
  onTested,
}: Props) {
  const [config, setConfig] = useState<Record<string, unknown>>(existingConfig)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [proxyOpen, setProxyOpen] = useState(false)
  const [step, setStep] = useState(0)
  // what is stored: the test reads it, so Verify opens only while the form matches
  const [saved, setSaved] = useState<Record<string, unknown>>(existingConfig)
  const [probe, setProbe] = useState<Probe>({ state: 'idle' })
  const [tested, setTested] = useState<LastTest | undefined>(lastTest)

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

  useEffect(() => {
    if (variant !== 'drawer') return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [variant, onClose])

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
      const saved = secretsSet[f.name] === true
      return (
        <Field key={f.name} label={f.label} hint={f.helpText}>
          <PasswordInput
            value={String(value)}
            placeholder={saved ? '•••••••• saved — leave blank to keep' : f.placeholder}
            onChange={(e) => set(f.name, e.target.value)}
          />
        </Field>
      )
    }
    return (
      <Field key={f.name} label={f.label} hint={f.helpText}>
        <TextInput value={String(value)} placeholder={f.placeholder} onChange={(e) => set(f.name, e.target.value)} />
      </Field>
    )
  }

  // A stored secret satisfies its required-check (it comes back redacted and is
  // kept when left blank) — key off the per-field "is set" signal, not the value.
  const missingRequired = fields.filter((f) => {
    if (!f.required) return false
    const v = config[f.name] ?? f.default
    const hasValue = v !== undefined && v !== ''
    return !hasValue && !(f.type === 'password' && secretsSet[f.name] === true)
  })

  const runTest = async () => {
    setProbe({ state: 'running' })
    try {
      const { data } = await configApi.testIntegration(integration.id)
      setProbe({ state: 'done', result: data })
      // not_testable has no probe to record; every other answer was stored
      if (data.reason !== 'not_testable') {
        setTested({ at: new Date().toISOString(), success: data.success, error: data.success ? null : data.message ?? null })
        onTested?.()
      }
    } catch (e) {
      const status = (e as { response?: { status?: number } }).response?.status
      setProbe({ state: 'error', message: extractApiError(e, 'The test could not run'), notConfigured: status === 400 })
    }
  }

  const dirty = JSON.stringify(config) !== JSON.stringify(saved)
  const configured = Object.keys(saved).length > 0 || Object.values(secretsSet).some(Boolean)
  const canVerify = configured && !dirty

  const handleSave = async () => {
    if (missingRequired.length) {
      setError(`Please fill in: ${missingRequired.map((f) => f.label).join(', ')}`)
      return
    }
    setSaving(true)
    setError(null)
    try {
      await onSave(integration.id, config)
      if (variant === 'setup') {
        onClose()
        return
      }
      setSaved(config)
      setStep(1)
      void runTest()
    } catch (e) {
      setError(extractApiError(e, 'Failed to save configuration'))
    } finally {
      setSaving(false)
    }
  }

  const form = (
    <>
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
    </>
  )
  const intro = (
    <>
      <p className="text-sm text-tx-3 leading-relaxed">{integration.description}</p>
      {integration.docs_url && (
        <a className="text-xs text-accent-2 inline-flex items-center gap-1 -mt-1" href={integration.docs_url} target="_blank" rel="noreferrer">
          <Icon name="link" size={12} /> Documentation
        </a>
      )}
    </>
  )

  if (variant === 'setup') {
    return (
      <Popup open onClose={onClose} title={`Configure ${integration.name}`} width={520}>
        <div className="flex flex-col gap-3.5">
          {intro}
          {form}
          <div className="flex justify-end gap-2.5 mt-1">
            <button className="btn ghost" onClick={onClose} disabled={saving}>Cancel</button>
            <button className="btn primary" onClick={handleSave} disabled={saving}>
              <Icon name="check2" /> {saving ? 'Saving…' : 'Save Configuration'}
            </button>
          </div>
        </div>
      </Popup>
    )
  }

  const level: Level = tested?.success === true ? 'good' : tested?.success === false ? 'poor' : null
  const hasSecret = fields.some((f) => f.type === 'password')
  const lastRead = tested ? `last read ${relativeTime(tested.at)}` : 'never tested'

  return (
    <div className="vg-skill-scrim" onMouseDown={onClose}>
      <aside
        className="vg-skill-drawer"
        role="dialog"
        aria-label={`Set up ${integration.name}`}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="int-drawer-head">
          <span className="flex flex-col gap-[3px] grow min-w-0">
            <span className="int-drawer-title">
              {integration.name}
              <LevelBadge level={level} variant="pill" />
            </span>
            <span className="vg-skill-hint">{[category, lastRead].filter(Boolean).join(' · ')}</span>
          </span>
          <button type="button" className="vg-skill-close" aria-label="Close" onClick={onClose}>
            <Icon name="close" size={16} />
          </button>
        </div>

        <div className="int-steps" role="tablist" aria-label="Setup steps">
          {STEPS.map((label, i) => (
            <button
              key={label}
              role="tab"
              aria-selected={step === i}
              className={`int-step${i === 0 && configured && !dirty ? ' done' : ''}`}
              disabled={i === 1 && !canVerify}
              title={i === 1 && !canVerify ? 'Save the connection first' : label}
              onClick={() => {
                setStep(i)
                if (i === 1 && probe.state === 'idle') void runTest()
              }}
            >
              <span className="int-step-n">{i === 0 && configured && !dirty ? <Icon name="check2" size={11} /> : i + 1}</span>
              <span>{label}</span>
            </button>
          ))}
        </div>

        {step === 0 && (
          <>
            {intro}
            {form}
          </>
        )}
        {step === 1 && <VerifyStep probe={probe} onRun={runTest} />}
        {step === 2 && (
          <StepLink to="/settings?section=federation" label="Open Alert collection">
            Pull alerts from this tool on a schedule so agents can start on them. That is set in Alert collection.
          </StepLink>
        )}
        {step === 3 && (
          <StepLink to="/settings?section=autoinvestigate" label="Open Limits & autonomy">
            Choose whether agents start on new alerts by themselves, and how much they may run and spend. That is set in Limits &amp; autonomy.
          </StepLink>
        )}

        {hasSecret && (
          <div className="int-sec">
            <span className="int-sec-h">Security controls</span>
            <span className="int-sec-row">
              <Icon name="check2" size={13} />
              Secrets are stored encrypted and never shown again; a blank field keeps the saved one.
            </span>
          </div>
        )}

        <div className="vg-skill-foot">
          <button type="button" className="vg-skill-btn" onClick={onClose} disabled={saving}>
            {step === 0 ? 'Cancel' : 'Close'}
          </button>
          {step === 0 && (
            <button type="button" className="vg-skill-btn primary" onClick={handleSave} disabled={saving}>
              {saving ? 'Saving…' : 'Save and verify'}
            </button>
          )}
        </div>
      </aside>
    </div>
  )
}

function StepLink({ to, label, children }: { to: string; label: string; children: string }) {
  return (
    <div className="flex flex-col gap-3 items-start">
      <p className="text-[13px] text-tx-2 leading-[1.5]">{children}</p>
      <Link className="btn" to={to}>{label}</Link>
    </div>
  )
}

/** Step 2: POST .../test probes the stored config; one row per MCP server behind the integration. */
function VerifyStep({ probe, onRun }: { probe: Probe; onRun: () => void }) {
  const again = (
    <div>
      <button className="btn" onClick={onRun} disabled={probe.state === 'running'}>
        <Icon name="refresh" /> {probe.state === 'error' || probe.state === 'done' ? 'Run the test again' : 'Run the test'}
      </button>
    </div>
  )
  const note = (text: string) => <p className="text-[13px] text-tx-2 leading-[1.5]">{text}</p>
  if (probe.state === 'idle') return <>{note('Vigil connects to each server behind this integration with the saved settings and shows whether it answered.')}{again}</>
  if (probe.state === 'running') return <p className="text-[13px] text-tx-3" role="status">Running the test…</p>
  if (probe.state === 'error') {
    return (
      <>
        <Banner kind="err">
          {probe.notConfigured ? 'Nothing is saved for this integration yet. Save the connection in step 1, then run the test.' : probe.message}
        </Banner>
        {again}
      </>
    )
  }
  const { result } = probe
  if (result.reason === 'not_testable') return note('This integration has no server Vigil can test.')
  const servers = result.servers ?? []
  return (
    <>
      {note('Vigil connects to each server behind this integration with the saved settings and shows whether it answered.')}
      {servers.length === 0 && <Banner kind="err">{result.message || 'Nothing was tested.'}</Banner>}
      {servers.map((sv) => (
        <div key={sv.name} className={`int-probe ${sv.success ? 'pass' : 'fail'}`}>
          <span className="int-probe-ic"><Icon name={sv.success ? 'check2' : 'close'} size={15} /></span>
          <span className="int-probe-name">{sv.name}</span>
          <span className="int-probe-detail" style={sv.success ? { color: 'var(--tx2)' } : undefined}>
            {sv.success ? 'Connected' : [sv.error, sv.missing_credentials?.length ? `Missing ${sv.missing_credentials.join(', ')}` : ''].filter(Boolean).join(' · ') || 'Failed'}
          </span>
        </div>
      ))}
      {again}
    </>
  )
}
