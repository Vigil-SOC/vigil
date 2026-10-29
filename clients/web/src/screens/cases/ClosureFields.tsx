import { useEffect, useState } from 'react'
import { casesApi } from '../../services/api'
import { Popup, Select } from '../../shared/ui'
import { inputCls } from './CaseSections'
import { errorDetail } from './caseFormat'
import { CLOSURE_CATEGORIES } from './useCases'

/** What a closer says a Case turned out to be. `duplicate` is bookkeeping rather
 *  than a determination, so it records a category and mints no verdict. */
export type ClosureCategory = (typeof CLOSURE_CATEGORIES)[number]['value']

/** The determination the epic exists for. "It is benign" with nothing behind it
 *  is the one claim nobody can weigh later, so it is the one reason required. */
export function closureProblem(category: ClosureCategory, reason: string): string | null {
  return category === 'false_positive' && !reason.trim()
    ? 'Say why this is a false positive — it becomes the reason on the record.'
    : null
}

/** The body for `casesApi.closeCase`: a false positive's reason is its own field. */
export function closurePayload(category: ClosureCategory, reason: string) {
  const said = reason.trim() || undefined
  return {
    closure_category: category,
    ...(category === 'false_positive' ? { false_positive_reason: said } : { closure_notes: said }),
  }
}

/** The category and reason a Case is closed with; shared by Edit and Close so the rule has one home. */
export function ClosureFields({ category, reason, onCategory, onReason }: {
  category: ClosureCategory
  reason: string
  onCategory: (c: ClosureCategory) => void
  onReason: (r: string) => void
}) {
  return (
    <>
      <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
        <span>What it turned out to be</span>
        <Select value={category} onSelect={(v) => onCategory(v as ClosureCategory)} options={[...CLOSURE_CATEGORIES]} />
      </label>
      <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
        <span>{category === 'false_positive' ? 'Why it is benign' : 'Reason'}</span>
        <textarea
          className={inputCls}
          rows={3}
          value={reason}
          onChange={(e) => onReason(e.target.value)}
          placeholder={category === 'false_positive'
            ? 'The scanner runs from that host every Sunday…'
            : 'Optional — what a reader should know about how this ended.'}
          style={{ resize: 'vertical' }}
        />
        <span className="text-[11.5px] font-normal normal-case tracking-normal text-tx-3 leading-[1.45]">
          {category === 'duplicate'
            ? 'Recorded on the case as bookkeeping. A duplicate is not a finding about the estate, so it adds nothing to memory.'
            : 'Kept as the reason on the record, and read back as why this case ended.'}
        </span>
      </label>
    </>
  )
}

/** Close an open Case from its page. Calls only the close endpoint, which sets the status itself. */
export function CloseCaseDialog({ open, id, onClose, onClosed }: { open: boolean; id: string; onClose: () => void; onClosed: () => void }) {
  const [category, setCategory] = useState<ClosureCategory>('resolved')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    if (open) {
      setCategory('resolved')
      setReason('')
      setErr('')
    }
  }, [open])

  const submit = async () => {
    const problem = closureProblem(category, reason)
    if (problem) { setErr(problem); return }
    setBusy(true)
    setErr('')
    try {
      await casesApi.closeCase(id, closurePayload(category, reason))
      onClosed()
      onClose()
    } catch (e) {
      setErr(errorDetail(e, 'Couldn’t close the case'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={onClose} title="Close case" width={480}>
      <div className="flex flex-col gap-3.5">
        <ClosureFields category={category} reason={reason} onCategory={setCategory} onReason={setReason} />
        {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={submit} disabled={busy}>{busy ? 'Closing…' : 'Close case'}</button>
        </div>
      </div>
    </Popup>
  )
}
