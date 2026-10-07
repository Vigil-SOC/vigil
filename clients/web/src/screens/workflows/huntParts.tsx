/* Pieces of a hunt that the run page, the History detail and the screen all read. They
   live apart from WorkflowsScreen so WatchPanels need not import the screen that renders it. */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { TextInput } from '../../shared/ui'
import { workflowApi } from '../../services/api'
import { errMsg, type HuntEvidence, type HuntGap, type HuntView } from './runRead'

/** A gap the projection reports live. query_intent belongs to the dispatch, which
 *  the finalized report joins in and a live read cannot, so the summary carries it. */
export function liveGap(one: HuntEvidence): HuntGap {
  return {
    evidence_id: one.evidence_id,
    iteration: one.iteration,
    summary: one.summary,
    hypothesis_id: one.bears_on?.[0]?.hypothesis_id ?? null,
  }
}

/** How the evidence landed on each belief, counted from the rulings the projection
 *  already carries. Every other field on a standing is written at verdict time, so an
 *  unresolved board reported the coerced status and nothing else — nine rows saying
 *  "inconclusive" over a run that had four records supporting one of them. */
export interface Bearing { supports: number; weakens: number; ruledOut: number }

export function bearings(evidence: readonly HuntEvidence[]): Map<string, Bearing> {
  const held = new Map<string, Bearing>()
  for (const record of evidence) {
    for (const link of record.bears_on ?? []) {
      const tally = held.get(link.hypothesis_id) ?? { supports: 0, weakens: 0, ruledOut: 0 }
      if (link.relation === 'supports') tally.supports += 1
      else if (link.relation === 'weakens') tally.weakens += 1
      else tally.ruledOut += 1
      held.set(link.hypothesis_id, tally)
    }
  }
  return held
}

/** Which belief the operator put up and which is the base rate to beat; any other source is untagged. */
export function provenanceTag(provenance?: string): { text: string; title?: string } | null {
  if (provenance === 'operator') return { text: 'yours' }
  if (provenance === 'base_rate') return { text: 'the claim to beat', title: 'Seeded on every hunt as the claim to beat, not something you asked for.' }
  return null
}

/** The four checkpoint classes a hunt can raise (services/agent/workflows/hunt/checkpoints.ts),
 *  in plain words. The buttons are labels only: the two answers stay approve and reject. */
type CheckpointClass = 'hypothesis_approval' | 'scope_extension' | 'verdict_review' | 'budget_anomaly'
interface ClassWords { why: string; approve: string; reject: string }
const CLASS_WORDS: Record<CheckpointClass, ClassWords> = {
  hypothesis_approval: { why: 'It wants your go-ahead on the explanations it will test.', approve: 'Approve explanations', reject: 'Stop the hunt' },
  scope_extension: { why: 'It reached its scope limit and wants to look further.', approve: 'Widen scope', reject: 'Keep scope' },
  verdict_review: { why: 'It wants a person to confirm what it found before it is recorded.', approve: 'Confirm verdict', reject: 'Keep looking' },
  budget_anomaly: { why: 'It asked for a person because spending or progress looks unusual.', approve: 'Carry on', reject: 'Not convinced' },
}
// a run recorded by an older agent service may carry a class this build does not know
const GENERIC_WORDS: ClassWords = { why: 'It stopped to ask you before going on.', approve: 'Approve', reject: 'Reject' }
const wordsOf = (klass?: string): ClassWords =>
  klass !== undefined && Object.keys(CLASS_WORDS).includes(klass) ? CLASS_WORDS[klass as CheckpointClass] : GENERIC_WORDS

/** The board's "Stopped · needs you" card: the one thing waiting on a person, and the
 *  approve/reject that answers it. Shown on the run page at the newest step, and in History. */
export function OpenCheckpoint({ hunt }: { hunt: HuntView }) {
  const open = hunt.open_checkpoint
  const [busy, setBusy] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  // Which question was answered: the projection reports it until the run journals a resolution.
  const [answered, setAnswered] = useState<{ checkpoint_id: string; kind: string } | null>(null)
  const [why, setWhy] = useState('')
  if (!open) return null

  const answer = (kind: 'approve' | 'reject') => {
    setBusy(kind)
    setFailed(null)
    workflowApi
      .steer(hunt.run_id ?? '', kind, why.trim(), { checkpoint_id: open.checkpoint_id })
      .then(() => { setAnswered({ checkpoint_id: open.checkpoint_id, kind }); setWhy('') })
      .catch((e) => setFailed(errMsg(e)))
      .finally(() => setBusy(null))
  }

  // Answered and waiting on the run, not on a person; clears when the ledger catches up.
  if (answered?.checkpoint_id === open.checkpoint_id) {
    return (
      <div className="flex items-center gap-2 p-3.5 rounded-[14px] text-[12px] border" style={{ background: 'var(--good-bg)', borderColor: 'var(--good-ln)' }}>
        <span className="inline-flex" style={{ color: 'var(--good)' }}><Icon name="check" size={15} /></span>
        <span><b>{answered.kind}</b> sent. The run picks it up at its next turn.</span>
      </div>
    )
  }

  const words = wordsOf(open.checkpoint_class)
  const unbound = (open.context?.['unbound_capabilities'] as string[] | undefined) ?? []
  const button = 'inline-flex items-center justify-center h-7 px-2.5 rounded-[10px] border text-[12px] font-semibold whitespace-nowrap cursor-pointer disabled:opacity-60 disabled:cursor-default'

  return (
    <div
      className="flex flex-col gap-2 p-3.5 rounded-[14px] border"
      style={{ background: 'var(--poor-bg)', borderColor: 'var(--poor-ln)' }}
    >
      <span className="text-[12px] font-bold text-[var(--poor)]">Stopped · needs you</span>
      <span className="text-[14px] font-bold text-[var(--tx0)] break-words" style={{ whiteSpace: 'pre-wrap' }}>{open.question}</span>
      <span className="text-[12px] leading-[1.45] text-[var(--tx2)]">
        {words.why}
        {unbound.length > 0 && ` No tool here answers ${unbound.join(', ')}.`}
      </span>
      <div className="flex gap-2 items-center flex-wrap">
        <button
          type="button" disabled={busy !== null} onClick={() => answer('approve')}
          className={button} style={{ background: 'var(--ac)', color: 'var(--ac-tx)', borderColor: 'var(--ac)' }}
        >
          {words.approve}
        </button>
        <button
          type="button" disabled={busy !== null} onClick={() => answer('reject')}
          className={`${button} bg-transparent text-[var(--tx0)] border-[var(--ln2)]`}
        >
          {words.reject}
        </button>
        <TextInput
          className="grow min-w-[140px]"
          placeholder="Why — recorded with your answer, and read by the run."
          value={why}
          onChange={(e) => setWhy(e.target.value)}
        />
      </div>
      {failed && <span className="text-[11.5px]" style={{ color: 'var(--poor)' }}>{failed}</span>}
    </div>
  )
}

export function hypothesisColor(s: string): string {
  if (s === 'proven' || s === 'handed_off') return 'var(--crit)'
  if (s === 'disproven') return 'var(--ok)'
  if (s === 'parked' || s === 'inconclusive') return 'var(--tx-2)'
  return 'var(--med)' // active
}
