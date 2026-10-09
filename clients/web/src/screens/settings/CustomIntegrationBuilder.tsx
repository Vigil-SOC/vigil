// Talks to /api/custom-integrations/* by fetch — those endpoints aren't in
// services/api.ts. Board: SettingsCustom.dc.html.
import { useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { InfoTip } from '../../shared/InfoTip'
import { Field, Select, TextInput } from '../../shared/ui'
import { basePath } from '../../config/basePath'
import { INTEGRATION_CATEGORIES } from '../../config/integrations'
import type { SectionProps } from './types'

interface Props {
  notify: SectionProps['notify']
  /** a file was written (Validate saves first): refetch the saved list */
  onWrote: () => void
  /** Save confirmed */
  onSaved: (integrationId: string) => void
}

interface SettingField {
  name: string
  label: string
  type: string
  required?: boolean
}

interface GeneratedIntegration {
  integration_id: string
  integration_name: string
  metadata: { category?: string; description?: string; fields?: SettingField[] }
  tools?: { name: string; description?: string }[]
  server_code: string
}

interface Validation {
  valid?: boolean
  checks?: Record<string, boolean>
  syntax_error?: string
}

const STEPS = ['Provide documentation', 'Review and edit', 'Test and save']
const CATEGORY_OPTIONS = INTEGRATION_CATEGORIES.map((c) => ({ value: c, label: c }))
const JSON_POST = { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' } } as const

const chipLabel = (f: SettingField) => (f.type === 'password' ? `${f.label} (secret)` : f.label)
const checkLabel = (k: string) => k.replace(/_/g, ' ').replace(/^\w/, (l) => l.toUpperCase())

export default function CustomIntegrationBuilder({ notify, onWrote, onSaved }: Props) {
  const [busy, setBusy] = useState<'generate' | 'validate' | 'save' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [documentation, setDocumentation] = useState('')
  const [integrationName, setIntegrationName] = useState('')
  const [category, setCategory] = useState('Custom')
  const fileRef = useRef<HTMLInputElement>(null)
  const [uploadedFile, setUploadedFile] = useState<File | null>(null)

  const [generated, setGenerated] = useState<GeneratedIntegration | null>(null)
  const [serverCode, setServerCode] = useState('')
  // the code last written to disk: Save only writes again when the code moved on
  const [savedCode, setSavedCode] = useState<string | null>(null)
  const [validation, setValidation] = useState<Validation | null>(null)

  const [conversation, setConversation] = useState<unknown[]>([])
  const [question, setQuestion] = useState<string | null>(null)
  const [userAnswer, setUserAnswer] = useState('')

  const loading = busy !== null
  const step = !generated ? 0 : validation ? 2 : 1

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploadedFile(file)
    const reader = new FileReader()
    reader.onload = (ev) => setDocumentation((ev.target?.result as string) || '')
    reader.readAsText(file)
  }

  const reset = () => {
    setError(null)
    setDocumentation('')
    setIntegrationName('')
    setCategory('Custom')
    setUploadedFile(null)
    setGenerated(null)
    setServerCode('')
    setSavedCode(null)
    setValidation(null)
    setConversation([])
    setQuestion(null)
    setUserAnswer('')
  }

  const generate = async (userResponse?: string) => {
    if (!documentation.trim() && !userResponse) { setError('Please provide API documentation.'); return }
    setBusy('generate')
    setError(null)
    try {
      const resp = await fetch(`${basePath}/api/custom-integrations/generate`, {
        ...JSON_POST,
        body: JSON.stringify({
          documentation,
          integration_name: integrationName || null,
          category,
          conversation_history: conversation.length ? conversation : null,
          user_response: userResponse || null,
        }),
      })
      const result = await resp.json()
      if (!resp.ok || !result.success) throw new Error(result.detail || result.error || 'Failed to generate integration')
      if (result.needs_clarification) {
        setQuestion(result.message)
        setConversation(result.conversation_history || [])
        setUserAnswer('')
      } else {
        setGenerated(result)
        setServerCode(result.server_code)
        setSavedCode(null)
        setValidation(null)
        setQuestion(null)
      }
    } catch (e) {
      setError((e as { message?: string })?.message || 'Failed to generate integration')
    } finally {
      setBusy(null)
    }
  }

  // writes the server file and metadata, unless this exact code is already on disk
  const write = async (g: GeneratedIntegration) => {
    if (savedCode === serverCode) return false
    const resp = await fetch(`${basePath}/api/custom-integrations/save`, {
      ...JSON_POST,
      body: JSON.stringify({ integration_id: g.integration_id, metadata: g.metadata, server_code: serverCode }),
    })
    if (!resp.ok) throw new Error('Failed to save integration')
    setSavedCode(serverCode)
    return true
  }

  const validate = async () => {
    if (!generated) return
    setBusy('validate')
    setError(null)
    try {
      const wrote = await write(generated)
      if (wrote) onWrote()
      const res = await fetch(`${basePath}/api/custom-integrations/${generated.integration_id}/validate`, {
        method: 'POST',
        credentials: 'include',
      })
      if (!res.ok) throw new Error('Failed to validate integration')
      setValidation(await res.json())
    } catch (e) {
      setError((e as { message?: string })?.message || 'Failed to validate integration')
    } finally {
      setBusy(null)
    }
  }

  const save = async () => {
    if (!generated) return
    setBusy('save')
    setError(null)
    try {
      await write(generated)
      notify('ok', `Custom integration "${generated.integration_id}" saved but not enabled. Configure it under Integrations to turn it on.`)
      onSaved(generated.integration_id)
      reset()
    } catch (e) {
      setError((e as { message?: string })?.message || 'Failed to save integration')
    } finally {
      setBusy(null)
    }
  }

  const fields = generated?.metadata?.fields ?? []
  const tools = generated?.tools ?? []

  return (
    <section className="cb" aria-label="Build a custom integration">
      <div className="cb-head">
        <h3 className="cb-title">Build a custom integration</h3>
        <p className="cb-sub">
          For a tool Vigil does not support yet. Paste its API documentation; Vigil drafts the connector and you review and
          edit it. Nothing is enabled until you configure it under Integrations.
        </p>
      </div>

      <ol className="cb-steps" aria-label="Steps">
        {STEPS.map((s, i) => (
          <li key={s} className={`cb-step${i === step ? ' cur' : i < step ? ' done' : ''}`} aria-current={i === step ? 'step' : undefined}>
            <span className="cb-step-n">{i < step ? <Icon name="check2" size={12} /> : i + 1}</span>
            {s}
          </li>
        ))}
      </ol>

      {error && <div className="settings-banner err" role="alert"><Icon name="alert" size={14} /> {error}</div>}

      <div className="cb-cols">
        {/* once drafted, the inputs are what the draft was made from: Start over to change them */}
        <fieldset className="cb-form" disabled={!!generated || !!question || loading}>
          <Field label="Category">
            <Select value={category} options={CATEGORY_OPTIONS} onSelect={setCategory} />
          </Field>
          <Field label="Name" hint="Leave blank and Vigil names it from the documentation.">
            <TextInput value={integrationName} placeholder="e.g. Acme XDR" onChange={(e) => setIntegrationName(e.target.value)} />
          </Field>
          <Field label="API documentation" hint="Paste text, or upload a documentation file.">
            <textarea
              className="field-input cb-docs"
              value={documentation}
              onChange={(e) => setDocumentation(e.target.value)}
              placeholder={'Paste API documentation here…\n\nInclude: endpoints, auth details, request/response examples, parameter descriptions.'}
            />
          </Field>
          <div className="cb-actions">
            <input ref={fileRef} type="file" hidden accept=".txt,.md,.pdf,.doc,.docx" onChange={onFile} aria-label="Documentation file" />
            <button type="button" className="btn ghost int-act" onClick={() => fileRef.current?.click()}>
              <Icon name="upload" /> <span className="cb-file">{uploadedFile ? uploadedFile.name : 'Upload documentation'}</span>
            </button>
            {!question && !generated && (
              <button type="button" className="btn primary int-act" onClick={() => generate()} disabled={!documentation.trim()}>
                {busy === 'generate' ? 'Generating…' : 'Generate draft'}
              </button>
            )}
          </div>
        </fieldset>

        <div className="cb-draft">
          <span className="cb-draft-title">Vigil’s draft</span>

          {!generated && !question && (
            <p className="cb-sub" role={busy === 'generate' ? 'status' : undefined}>
              {busy === 'generate'
                ? 'Vigil is reading the documentation and drafting the connector…'
                : 'Nothing drafted yet. Add the API documentation and choose Generate draft: the settings it will ask for, the tools agents will get and the code appear here.'}
            </p>
          )}

          {!generated && question && (
            <>
              <div className="settings-banner info"><Icon name="info" size={14} /> Vigil needs more information. Answer the question below.</div>
              <p className="cb-question">{question}</p>
              <Field label="Your answer">
                <textarea
                  className="field-input cb-answer"
                  value={userAnswer}
                  onChange={(e) => setUserAnswer(e.target.value)}
                  placeholder="Be as specific as possible to help Vigil draft the best integration."
                />
              </Field>
              <div className="cb-actions">
                <button type="button" className="btn primary int-act" onClick={() => generate(userAnswer)} disabled={loading || !userAnswer.trim()}>
                  {busy === 'generate' ? 'Sending…' : 'Send answer'}
                </button>
                <button type="button" className="btn ghost int-act cb-reset" onClick={reset} disabled={loading}>Start over</button>
              </div>
            </>
          )}

          {generated && (
            <>
              <div className="cb-meta">
                <b>{generated.integration_name}</b>
                <code>{generated.integration_id}</code>
              </div>
              {generated.metadata?.description && <p className="cb-sub">{generated.metadata.description}</p>}

              <span className="cb-label">Settings it will ask for</span>
              {fields.length ? (
                <span className="cb-chips">
                  {fields.map((f) => <span key={f.name} className="cb-chip">{chipLabel(f)}</span>)}
                </span>
              ) : (
                <span className="cb-sub">None.</span>
              )}

              <span className="cb-label">Tools agents will get</span>
              {tools.length ? (
                <ul className="cb-tools">
                  {tools.map((t) => (
                    <li key={t.name} title={t.description || t.name}>
                      <code>{t.name}</code>
                      {t.description && <span className="cb-tool-d">{t.description}</span>}
                    </li>
                  ))}
                </ul>
              ) : (
                <span className="cb-sub">Vigil did not list any tools. Check the code below.</span>
              )}

              <label className="cb-label" htmlFor="cb-code">Server code</label>
              <textarea
                id="cb-code"
                className="cb-code"
                spellCheck={false}
                value={serverCode}
                onChange={(e) => {
                  setServerCode(e.target.value)
                  setValidation(null) // the result belongs to the code it checked
                }}
              />

              {validation && (
                <div className="cb-result" role="status">
                  <div className={`settings-banner ${validation.valid ? 'ok' : 'err'}`}>
                    <Icon name={validation.valid ? 'check2' : 'alert'} size={14} />
                    <span>{validation.valid ? 'The code passes the static check.' : 'The static check found problems. Edit the code, then validate again.'}</span>
                  </div>
                  {validation.checks && (
                    <ul className="cb-checks">
                      {Object.entries(validation.checks).map(([k, v]) => (
                        <li key={k} className={v ? '' : 'bad'}>
                          <Icon name={v ? 'check2' : 'alert'} size={13} /> {checkLabel(k)}
                        </li>
                      ))}
                    </ul>
                  )}
                  {validation.syntax_error && <code className="cb-syntax">{validation.syntax_error}</code>}
                </div>
              )}
              {savedCode !== null && (
                <p className="cb-sub">
                  {savedCode === serverCode
                    ? 'Saved, not enabled. Configure it under Integrations to turn it on.'
                    : 'You changed the code since it was saved. Save writes it again.'}
                </p>
              )}

              <div className="cb-actions">
                <button type="button" className="btn ghost int-act" onClick={validate} disabled={loading}>
                  {busy === 'validate' ? 'Validating…' : 'Validate'}
                </button>
                <InfoTip
                  label="About Validate"
                  align="start"
                  source="The generated code: it must compile and define the MCP server entry points."
                  limit="A static check that never calls the target API. Validate also saves the integration; it stays off until you configure it under Integrations."
                />
                <button type="button" className="btn primary int-act" onClick={save} disabled={loading}>
                  {busy === 'save' ? 'Saving…' : 'Save'}
                </button>
                <button type="button" className="btn ghost int-act cb-reset" onClick={reset} disabled={loading}>Start over</button>
              </div>
            </>
          )}
        </div>
      </div>
    </section>
  )
}
