import { useEffect, useRef, useState } from 'react'
import { Icon } from '../../shared/icons'
import { Popup } from '../../shared/ui'
import { workflowApi, type GeneratedDraft } from '../../services/api'

function errMsg(e: unknown): string {
  const r = e as { response?: { data?: { detail?: string } }; message?: string }
  return r?.response?.data?.detail || r?.message || 'Something went wrong'
}

/** One prompt field. The result opens in the reader pane as an AI draft; nothing is saved here. */
export default function DescribeDialog({ onClose, onDrafted }: { onClose: () => void; onDrafted: (draft: GeneratedDraft) => void }) {
  const [prompt, setPrompt] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ready = prompt.trim() !== '' && !busy
  // a draft that arrives after Cancel is dropped, not opened
  const open = useRef(true)
  useEffect(() => { open.current = true; return () => { open.current = false } }, [])

  const generate = () => {
    setBusy(true)
    setError(null)
    workflowApi.generate(prompt.trim())
      .then((res) => {
        const draft = res.data?.draft
        if (!draft) throw new Error('No draft came back. Try describing it differently.')
        if (open.current) onDrafted(draft)
      })
      .catch((e) => { if (open.current) { setError(errMsg(e)); setBusy(false) } })
  }

  return (
    <Popup open onClose={onClose} title="Generate with AI" width={560}>
      <div className="flex flex-col gap-3.5">
        <label className="flex flex-col gap-1.5">
          <span className="text-[11px] uppercase tracking-[0.06em] text-tx-3">Scenario description</span>
          <textarea
            autoFocus
            rows={4}
            className="w-full bg-bg border border-line rounded-[7px] px-2.5 py-2 text-[13px] text-tx outline-none focus:border-accent-line resize-y max-w-full"
            placeholder="e.g. Investigate and contain a ransomware incident on an endpoint"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
          <span className="text-[11px] text-tx-3">Drafts the whole workflow. You read it before anything is saved.</span>
        </label>
        {error && <div role="alert" className="text-[12.5px]" style={{ color: 'var(--crit)' }}>{error}</div>}
        <div className="flex justify-end gap-2.5 pt-1">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={!ready} style={{ opacity: ready ? 1 : 0.5 }} onClick={generate}><Icon name="sparkle" /> {busy ? 'Generating…' : 'Generate'}</button>
        </div>
      </div>
    </Popup>
  )
}
