/* ============================================================
   AI-powered custom integration builder (redesign port). 3 steps:
   provide docs → review/edit generated metadata + MCP server code →
   validate & save. Talks to /api/custom-integrations/* directly via
   fetch (these endpoints aren't in services/api.ts).
   ============================================================ */
import { useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Field, Popup, Select, TextInput } from '../../shared/ui'
import { basePath } from '../../../config/basePath'
import { INTEGRATION_CATEGORIES } from '../../../config/integrations'

interface Props {
  onClose: () => void
  onSave: (integrationId: string) => void
}

interface GeneratedIntegration {
  integration_id: string
  integration_name: string
  metadata: { category?: string; description?: string; fields?: { name: string; label: string; type: string; required?: boolean }[] }
  server_code: string
}

const STEPS = ['Provide Documentation', 'Review & Edit', 'Test & Save']
const CATEGORY_OPTIONS = INTEGRATION_CATEGORIES.map((c) => ({ value: c, label: c }))

export default function CustomIntegrationBuilder({ onClose, onSave }: Props) {
  const [step, setStep] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [documentation, setDocumentation] = useState('')
  const [integrationName, setIntegrationName] = useState('')
  const [category, setCategory] = useState('Custom')
  const fileRef = useRef<HTMLInputElement>(null)
  const [uploadedFile, setUploadedFile] = useState<File | null>(null)

  const [generated, setGenerated] = useState<GeneratedIntegration | null>(null)
  const [serverCode, setServerCode] = useState('')

  const [needsClarification, setNeedsClarification] = useState(false)
  const [conversation, setConversation] = useState<unknown[]>([])
  const [claudeQuestion, setClaudeQuestion] = useState('')
  const [userAnswer, setUserAnswer] = useState('')

  const [validation, setValidation] = useState<{ valid?: boolean; checks?: Record<string, boolean>; syntax_error?: string } | null>(null)
  const [showCode, setShowCode] = useState(false)

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploadedFile(file)
    const reader = new FileReader()
    reader.onload = (ev) => setDocumentation((ev.target?.result as string) || '')
    reader.readAsText(file)
  }

  const generate = async (userResponse?: string) => {
    if (!documentation.trim() && !userResponse) { setError('Please provide API documentation.'); return }
    setLoading(true)
    setError(null)
    try {
      const resp = await fetch(`${basePath}/api/custom-integrations/generate`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
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
        setNeedsClarification(true)
        setClaudeQuestion(result.message)
        setConversation(result.conversation_history || [])
        setUserAnswer('')
      } else {
        setGenerated(result)
        setServerCode(result.server_code)
        setNeedsClarification(false)
        setStep(1)
      }
    } catch (e) {
      setError((e as { message?: string })?.message || 'Failed to generate integration')
    } finally {
      setLoading(false)
    }
  }

  const validate = async () => {
    if (!generated) return
    setLoading(true)
    setError(null)
    try {
      const save = await fetch(`${basePath}/api/custom-integrations/save`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ integration_id: generated.integration_id, metadata: generated.metadata, server_code: serverCode }),
      })
      if (!save.ok) throw new Error('Failed to save integration for validation')
      const res = await fetch(`${basePath}/api/custom-integrations/${generated.integration_id}/validate`, {
        method: 'POST',
        credentials: 'include',
      })
      if (!res.ok) throw new Error('Failed to validate integration')
      setValidation(await res.json())
      setStep(2)
    } catch (e) {
      setError((e as { message?: string })?.message || 'Failed to validate integration')
    } finally {
      setLoading(false)
    }
  }

  const finalSave = async () => {
    if (!generated) return
    setLoading(true)
    setError(null)
    try {
      const resp = await fetch(`${basePath}/api/custom-integrations/save`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ integration_id: generated.integration_id, metadata: generated.metadata, server_code: serverCode }),
      })
      if (!resp.ok) throw new Error('Failed to save integration')
      await resp.json()
      onSave(generated.integration_id)
    } catch (e) {
      setError((e as { message?: string })?.message || 'Failed to save integration')
    } finally {
      setLoading(false)
    }
  }

  const next = () => {
    if (step === 0) (needsClarification ? generate(userAnswer) : generate())
    else if (step === 1) validate()
    else finalSave()
  }

  const nextLabel = loading ? 'Processing…' : step === 2 ? 'Save Integration' : step === 1 ? 'Validate' : needsClarification ? 'Send Answer' : 'Generate'
  const nextDisabled =
    loading ||
    (step === 0 && !needsClarification && !documentation.trim()) ||
    (step === 0 && needsClarification && !userAnswer.trim())

  return (
    <Popup open onClose={onClose} title="AI-Powered Custom Integration Builder" width={720}>
      {/* step indicator */}
      <div className="flex items-center gap-2 mb-5">
        {STEPS.map((s, i) => (
          <div key={s} className="flex items-center gap-2">
            <span className={`flex items-center justify-center w-5 h-5 rounded-full text-[11px] font-semibold ${i === step ? 'bg-[var(--accent)] text-white' : i < step ? 'bg-[var(--accent-dim)] text-accent-2' : 'bg-[var(--bg-3)] text-tx-3'}`}>{i + 1}</span>
            <span className={`text-xs ${i === step ? 'text-tx' : 'text-tx-3'}`}>{s}</span>
            {i < STEPS.length - 1 && <span className="w-5 h-px bg-line" />}
          </div>
        ))}
      </div>

      {error && <div className="settings-banner err mb-3"><Icon name="alert" size={14} /> {error}</div>}

      {step === 0 && !needsClarification && (
        <div className="flex flex-col gap-3.5">
          <p className="text-sm text-tx-3">Paste API documentation or upload a file. The AI analyzes it and generates a complete integration: MCP server code + configuration.</p>
          <Field label="Integration Category">
            <Select value={category} options={CATEGORY_OPTIONS} onSelect={setCategory} />
          </Field>
          <Field label="Integration Name (optional)" hint="Leave blank to auto-generate from documentation.">
            <TextInput value={integrationName} placeholder="e.g. My Security Tool" onChange={(e) => setIntegrationName(e.target.value)} />
          </Field>
          <div>
            <input ref={fileRef} type="file" hidden accept=".txt,.md,.pdf,.doc,.docx" onChange={onFile} />
            <button className="btn ghost" onClick={() => fileRef.current?.click()}>
              <Icon name="upload" /> {uploadedFile ? uploadedFile.name : 'Upload Documentation File'}
            </button>
          </div>
          <Field label="API Documentation">
            <textarea
              className="field-input"
              style={{ minHeight: 200, fontFamily: 'var(--mono)', resize: 'vertical' }}
              value={documentation}
              onChange={(e) => setDocumentation(e.target.value)}
              placeholder={'Paste API documentation here…\n\nInclude: endpoints, auth details, request/response examples, parameter descriptions.'}
            />
          </Field>
        </div>
      )}

      {step === 0 && needsClarification && (
        <div className="flex flex-col gap-3.5">
          <div className="settings-banner info"><Icon name="info" size={14} /> Claude needs more information. Answer the question below.</div>
          <div className="card card-sq p-3.5">
            <div className="flex items-start gap-2.5">
              <span className="flex items-center justify-center w-8 h-8 rounded-full bg-[var(--accent)] text-white text-xs font-semibold flex-shrink-0">AI</span>
              <p className="text-sm text-tx whitespace-pre-wrap">{claudeQuestion}</p>
            </div>
          </div>
          <Field label="Your Answer">
            <textarea
              className="field-input"
              style={{ minHeight: 120, resize: 'vertical' }}
              value={userAnswer}
              onChange={(e) => setUserAnswer(e.target.value)}
              placeholder="Be as specific as possible to help Claude generate the best integration."
            />
          </Field>
        </div>
      )}

      {step === 1 && generated && (
        <div className="flex flex-col gap-3">
          <div className="settings-banner info"><Icon name="info" size={14} /> Review the generated integration. You can view and edit the server code before saving.</div>
          <div className="card card-sq p-3.5 flex flex-col gap-2.5">
            <div className="kv-grid" style={{ gridTemplateColumns: '120px 1fr' }}>
              <span className="k">Name</span><span className="v">{generated.integration_name}</span>
              <span className="k">ID</span><span className="v font-mono text-xs">{generated.integration_id}</span>
              <span className="k">Category</span><span className="v">{generated.metadata?.category}</span>
              <span className="k">Description</span><span className="v">{generated.metadata?.description}</span>
            </div>
            {generated.metadata?.fields?.length ? (
              <div>
                <span className="text-xs text-tx-3">Configuration Fields</span>
                <div className="flex gap-1.5 flex-wrap mt-1.5">
                  {generated.metadata.fields.map((f) => (
                    <span key={f.name} className={`chip${f.required ? ' sel' : ''}`}>{f.label} ({f.type})</span>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
          <div className="card card-sq p-3.5 flex items-center justify-between">
            <span className="text-sm text-tx flex items-center gap-2"><Icon name="doc" size={15} /> {serverCode.split('\n').length} lines of Python generated</span>
            <button className="btn ghost" onClick={() => setShowCode(true)}><Icon name="eye" /> View / edit code</button>
          </div>
        </div>
      )}

      {step === 2 && validation && (
        <div className="flex flex-col gap-3">
          <div className={`settings-banner ${validation.valid ? 'ok' : 'err'}`}>
            <Icon name={validation.valid ? 'check2' : 'alert'} size={14} />
            <span>{validation.valid ? 'Integration code is valid and ready to use!' : 'There are issues with the generated code — you may need to edit it manually.'}</span>
          </div>
          {validation.checks && (
            <div className="card card-sq p-3.5 flex flex-col gap-1.5">
              {Object.entries(validation.checks).map(([k, v]) => (
                <div key={k} className="flex items-center gap-2 text-sm">
                  <Icon name={v ? 'check2' : 'alert'} size={14} />
                  <span className={v ? 'text-tx-2' : 'text-crit'}>{k.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())}</span>
                </div>
              ))}
            </div>
          )}
          {validation.syntax_error && (
            <div className="settings-banner err"><Icon name="alert" size={14} /> <span className="font-mono text-xs">{validation.syntax_error}</span></div>
          )}
          <div className="settings-banner info">
            <Icon name="info" size={14} />
            <span>After saving: configure the integration under Integrations, enable it, and restart MCP servers if needed.</span>
          </div>
        </div>
      )}

      {/* footer */}
      <div className="flex justify-end gap-2.5 mt-6">
        <button className="btn ghost" onClick={onClose} disabled={loading}>Cancel</button>
        {step > 0 && <button className="btn ghost" onClick={() => { setStep(step - 1); setError(null) }} disabled={loading}>Back</button>}
        <button className="btn primary" onClick={next} disabled={nextDisabled}>{nextLabel}</button>
      </div>

      {/* nested code editor */}
      {showCode && (
        <Popup open onClose={() => setShowCode(false)} title="MCP Server Code" width={900}>
          <textarea
            className="field-input"
            style={{ minHeight: 480, fontFamily: 'var(--mono)', fontSize: 12.5, resize: 'vertical' }}
            value={serverCode}
            onChange={(e) => setServerCode(e.target.value)}
          />
          <div className="flex justify-end mt-4">
            <button className="btn primary" onClick={() => setShowCode(false)}>Done</button>
          </div>
        </Popup>
      )}
    </Popup>
  )
}
