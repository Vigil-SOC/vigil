import { useEffect, useState } from 'react'
import { casesApi } from '../../services/api'
import { FALSE_POSITIVE_CAUSES, type FalsePositiveCause } from '../../shared/falsePositiveCause'
import { Popup, Select } from '../../shared/ui'
import { inputCls } from './CaseSections'
import { errorDetail } from './caseFormat'
import { CLOSURE_CATEGORIES } from './useCases'

/** What a closer says a Case turned out to be. `duplicate` is bookkeeping rather
 *  than a determination, so it records a category and mints no verdict. */
export type ClosureCategory = (typeof CLOSURE_CATEGORIES)[number]['value']

/** A close form's category before anyone picks one. Nothing is preselected: a
 *  default would record a determination the closer never made. */
export type ClosureChoice = ClosureCategory | ''

/** The close stays disabled until a category is chosen; "Unable to resolve" is on offer so this never blocks. */
const CLOSURE_CHOICE_NEEDED = 'Choose what it turned out to be to close the case.'

/** The determination the epic exists for. "It is benign" with nothing behind it
 *  is the one claim nobody can weigh later, so it is the one reason required. */
export function closureProblem(category: ClosureChoice, reason: string): string | null {
  return category === 'false_positive' && !reason.trim()
    ? 'Say why this is a false positive — it becomes the reason on the record.'
    : null
}

/** The body for `casesApi.closeCase`: a false positive's reason is its own field.
 *  A false positive always states the cause, null when none was chosen, so a
 *  re-close does not keep a cause this person did not pick. */
export function closurePayload(category: ClosureCategory, reason: string, cause: string) {
  const said = reason.trim() || undefined
  const known = FALSE_POSITIVE_CAUSES.find((item) => item.value === cause)
  const stated: FalsePositiveCause | null = known ? known.value : null
  return {
    closure_category: category,
    ...(category === 'false_positive'
      ? { false_positive_reason: said, false_positive_cause: stated }
      : { closure_notes: said }),
  }
}

/** The category and reason a Case is closed with; shared by Edit and Close so the rule has one home. */
export function ClosureFields({ category, reason, cause, onCategory, onReason, onCause }: {
  category: ClosureChoice
  reason: string
  cause: string
  onCategory: (c: ClosureCategory) => void
  onReason: (r: string) => void
  onCause: (c: string) => void
}) {
  return (
    <>
      <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
        <span>What it turned out to be</span>
        <Select value={category} placeholder="Choose one" onSelect={(v) => onCategory(v as ClosureCategory)} options={[...CLOSURE_CATEGORIES]} />
        {!category && (
          <span className="text-[11.5px] font-normal normal-case tracking-normal text-tx-3 leading-[1.45]">{CLOSURE_CHOICE_NEEDED}</span>
        )}
      </label>
      {category === 'false_positive' && (
        <label className="flex flex-col gap-1.5 text-xs font-semibold uppercase tracking-wide text-tx-3">
          <span>Cause</span>
          <Select
            value={cause}
            placeholder="Optional"
            onSelect={onCause}
            options={[{ value: '', label: 'Not given' }, ...FALSE_POSITIVE_CAUSES]}
          />
          <span className="text-[11.5px] font-normal normal-case tracking-normal text-tx-3 leading-[1.45]">
            Optional. Which kind of fix this is. Nothing chosen is recorded as no cause.
          </span>
        </label>
      )}
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

/** Close a Case from its page, or re-close one the agent closed. The category starts empty every time.
 *  Calls only the close endpoint, which sets the status itself.
 *  `evidenceOpened` is sent only when the caller passes it. The list's close goes through `closurePayload` and leaves the field off. */
export function CloseCaseDialog({ open, id, title = 'Close case', evidenceOpened, onClose, onClosed }: {
  open: boolean
  id: string
  title?: string
  /** This session opened the Evidence section before the close. Omitted, the body does not carry the flag. */
  evidenceOpened?: boolean
  onClose: () => void
  onClosed: () => void
}) {
  const [category, setCategory] = useState<ClosureChoice>('')
  const [reason, setReason] = useState('')
  const [cause, setCause] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    if (open) {
      setCategory('')
      setReason('')
      setCause('')
      setErr('')
    }
  }, [open])

  const submit = async () => {
    if (!category) return
    const problem = closureProblem(category, reason)
    if (problem) { setErr(problem); return }
    setBusy(true)
    setErr('')
    try {
      await casesApi.closeCase(id, {
        ...closurePayload(category, reason, cause),
        ...(evidenceOpened === undefined ? {} : { evidence_opened: evidenceOpened }),
      })
      onClosed()
      onClose()
    } catch (e) {
      setErr(errorDetail(e, 'Couldn’t close the case'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={onClose} title={title} width={480}>
      <div className="flex flex-col gap-3.5">
        <ClosureFields category={category} reason={reason} cause={cause} onCategory={setCategory} onReason={setReason} onCause={setCause} />
        {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={submit} disabled={busy || !category}>{busy ? 'Closing…' : 'Close case'}</button>
        </div>
      </div>
    </Popup>
  )
}
