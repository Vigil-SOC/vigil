import { useCallback, useEffect, useRef, useState } from 'react'
import { format } from 'date-fns'
import { approvalsApi, casesApi, orchestratorApi, workflowApi, type CaseRecordRow, type NeedsYouItem } from '../../services/api'
import { NotMeasured } from '../../shared/NotMeasured'
import { HoldButton } from '../../shared/HoldButton'
import { Icon } from '../../shared/icons'
import { EmptyState } from '../../shared/ui'
import type { CaseRow } from '../../data/data'
import Chat from '../../shell/Chat'
import { CommentsCard, EvidenceCard, IOCsCard, TasksCard } from './CaseSections'
import {
  explanationWord,
  honestLine,
  readFold,
  recallEntityCalls,
  recordChip,
  visibilityGaps,
  type RecordChip,
  type RunFold,
} from './caseFold'
import type { CaseClosureView, CaseInvestigationRef, CaseLinkedFinding, Phase } from './useCases'

const TABS = ['Summary', 'Explanations', 'Evidence', 'Checked', 'Memory and blind spots', 'Record'] as const
type Tab = (typeof TABS)[number]
const NEEDS_POLL_MS = 20_000

const LATER = 'Later. Nothing writes this yet — it is the phase-2 Act contract.'
const CHAINED = 'Only the run’s rows are hash-chained. Case audit rows are not.'

function Mark({ text }: { text: string }) {
  return (
    <span className="case-mark" title={text} aria-label={text}>
      <Icon name="info" size={14} />
    </span>
  )
}

function when(value?: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : format(d, 'MMM d, yyyy · HH:mm')
}

function money(value: number | null | undefined): string {
  if (value == null) return '—'
  return `$${value.toFixed(4)}`
}

function latency(ms: number | undefined): string {
  return ms == null ? '—' : `${ms} ms`
}

function detailOf(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return (error as { message?: string })?.message || fallback
}

function LinkedFindings({ items }: { items: CaseLinkedFinding[] }) {
  if (items.length === 0) return null
  return (
    <ul className="case-linked">
      {items.map((item) => (
        <li key={item.finding_id}>
          <span>{item.description || item.finding_id}</span>
          {item.source_link && (
            <a href={item.source_link} target="_blank" rel="noreferrer">Open in source</a>
          )}
        </li>
      ))}
    </ul>
  )
}

function CaseNeeds({
  items,
  busy,
  error,
  onApprove,
  onReject,
}: {
  items: NeedsYouItem[]
  busy: boolean
  error: string | null
  onApprove: (id: string) => void
  onReject: (id: string, reason: string) => void
}) {
  if (items.length === 0) return null
  return (
    <section className="case-needs" aria-label="Needs you">
      {error && <p role="alert">{error}</p>}
      {items.map((item) => (
        <CaseNeed key={item.source_id} item={item} busy={busy} onApprove={onApprove} onReject={onReject} />
      ))}
    </section>
  )
}

function CaseNeed({
  item,
  busy,
  onApprove,
  onReject,
}: {
  item: NeedsYouItem
  busy: boolean
  onApprove: (id: string) => void
  onReject: (id: string, reason: string) => void
}) {
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')

  return (
    <article>
      <h3>{item.title}</h3>
      <p className="case-needs-meta">{item.kind} · {item.reversibility}</p>
      {item.reason && <p className="case-needs-reason">{item.reason}</p>}
      <div className="case-needs-actions">
        {item.reversibility === 'reversible' ? (
          <button type="button" className="btn primary" disabled={busy} onClick={() => onApprove(item.source_id)}>
            Approve
          </button>
        ) : (
          <HoldButton label="Approve" disabled={busy} onConfirm={() => onApprove(item.source_id)} />
        )}
        {rejecting ? (
          <form
            className="case-needs-reject"
            onSubmit={(event) => {
              event.preventDefault()
              const text = reason.trim()
              if (!text) return
              onReject(item.source_id, text)
            }}
          >
            <textarea
              className="feedback-box"
              aria-label="Rejection reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Why is this being rejected?"
              autoFocus
            />
            <button type="submit" className="btn danger" disabled={busy || !reason.trim()}>
              Reject
            </button>
          </form>
        ) : (
          <button type="button" className="btn danger" disabled={busy} onClick={() => setRejecting(true)}>
            Reject
          </button>
        )}
      </div>
    </article>
  )
}

function counts(fold: RunFold | null, record: number): Record<Tab, number> {
  const calls = fold?.calls.length ?? 0
  const memory = (fold?.recall ? 1 : 0) + recallEntityCalls(fold).length + visibilityGaps(fold).length
  if (fold?.kind === 'hunt') {
    return {
      Summary: fold.hypotheses.length,
      Explanations: fold.hypotheses.length,
      Evidence: fold.evidenceCount,
      Checked: calls,
      'Memory and blind spots': memory,
      Record: record,
    }
  }
  const findings = fold?.findings.length ?? 0
  return {
    Summary: findings,
    Explanations: 0,
    Evidence: findings,
    Checked: calls,
    'Memory and blind spots': memory,
    Record: record,
  }
}

/** ⋯ menu in the head row. Same pattern as the console's More menu: closes on outside click and Escape. */
function CaseMenu({ onEdit, onMerge, onDelete }: { onEdit: () => void; onMerge: () => void; onDelete?: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault() // the drawer's own Escape handler skips handled keys
      setOpen(false)
    }
    // capture: the drawer stops mousedown from bubbling to the document
    document.addEventListener('mousedown', onDoc, true)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const pick = (fn: () => void) => () => {
    setOpen(false)
    triggerRef.current?.focus() // the dialog returns focus here when it closes
    fn()
  }

  return (
    <div className="vg-more dh-more" ref={ref}>
      <button
        type="button"
        ref={triggerRef}
        className="btn ghost icon"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Case actions"
        title="Case actions"
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="more" size={15} />
      </button>
      {open && (
        <div className="vg-more-menu dh-menu" role="menu" aria-label="Case actions">
          <button type="button" role="menuitem" onClick={pick(onEdit)}><Icon name="edit" size={14} /> Edit</button>
          <button type="button" role="menuitem" onClick={pick(onMerge)}><Icon name="link" size={14} /> Merge</button>
          {onDelete && (
            <button type="button" role="menuitem" className="danger" onClick={pick(onDelete)}>
              <Icon name="trash" size={14} /> Delete case
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export function CasePage({
  id,
  c,
  created,
  combinedState,
  investigations,
  closure,
  linkedFindings,
  phase,
  error,
  pageKey,
  onBack,
  onExpand,
  onEdit,
  onMerge,
  onDelete,
  canDelete,
  onChanged,
}: {
  id: string
  c: CaseRow | null
  created: string
  combinedState: string
  investigations: CaseInvestigationRef[]
  closure: CaseClosureView | null
  linkedFindings: CaseLinkedFinding[]
  phase: Phase
  error: string | null
  /** Route key stored as page_context. Cases passes `cases`; the drawer passes SocConsole's current. */
  pageKey: string
  /** The "Cases" crumb; in the drawer it also backs the Close icon. */
  onBack: () => void
  /** Set only in the drawer: shows the Expand and Close icons. */
  onExpand?: () => void
  onEdit: () => void
  onMerge: () => void
  onDelete: () => void
  canDelete: boolean
  onChanged: () => void
}) {
  const [tab, setTab] = useState<Tab>('Summary')
  const [fold, setFold] = useState<RunFold | null>(null)
  const [foldPhase, setFoldPhase] = useState<Phase>('loading')
  const [sla, setSla] = useState<{ due: string; health: string } | null>(null)
  const [rows, setRows] = useState<CaseRecordRow[]>([])
  const [recordPhase, setRecordPhase] = useState<Phase>('loading')
  const [recordError, setRecordError] = useState<string | null>(null)
  const [recordKey, setRecordKey] = useState(0)
  const [chip, setChip] = useState<RecordChip | 'all'>('all')
  const [askSeed, setAskSeed] = useState<{ id: string; text: string } | null>(null)
  const [focusEvidence, setFocusEvidence] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [needsItems, setNeedsItems] = useState<NeedsYouItem[]>([])
  const [needsCount, setNeedsCount] = useState(0)
  const [decisionBusy, setDecisionBusy] = useState<string | null>(null)
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const decisionBusyRef = useRef<string | null>(null)
  const needsTicket = useRef(0)
  const seenCase = useRef(id)
  if (seenCase.current !== id) {
    seenCase.current = id
    needsTicket.current += 1
    decisionBusyRef.current = null
    setNeedsItems([])
    setNeedsCount(0)
    setDecisionBusy(null)
    setDecisionError(null)
  }

  const loadNeeds = useCallback(async () => {
    const ticket = ++needsTicket.current
    const forCase = id
    try {
      const res = await approvalsApi.needsYou(forCase)
      if (ticket !== needsTicket.current || forCase !== seenCase.current) return
      setNeedsItems(res.data.items)
      setNeedsCount(res.data.count)
    } catch {
      if (ticket !== needsTicket.current || forCase !== seenCase.current) return
    }
  }, [id])

  useEffect(() => {
    void loadNeeds()
    const timer = window.setInterval(() => {
      void loadNeeds()
    }, NEEDS_POLL_MS)
    return () => window.clearInterval(timer)
  }, [loadNeeds])

  useEffect(() => {
    setTab('Summary')
    setAskSeed(null)
    setFocusEvidence(null)
    setNote('')
    setChip('all')
  }, [id])

  const latest = investigations[0] ?? null
  const runId = latest?.run_id ?? null
  const live = investigations.filter((item) => item.live)
  // The list maps every status other than open/investigating to closed, so a
  // `new` case must not use that fallback while the detail read is in flight.
  const closed = combinedState === 'closed' || (phase === 'ready' && !combinedState && c?.status === 'closed')
  const pill = combinedState || (phase === 'loading' ? '…' : c?.status || '—')

  useEffect(() => {
    if (!runId) {
      setFold(null)
      setFoldPhase('ready')
      return
    }
    let cancelled = false
    setFold(null)
    setFoldPhase('loading')
    workflowApi
      .getRun(runId)
      .then((res) => {
        if (!cancelled) {
          setFold(readFold(res.data))
          setFoldPhase('ready')
        }
      })
      .catch(() => {
        if (!cancelled) {
          setFold(null)
          setFoldPhase('error')
        }
      })
    return () => {
      cancelled = true
    }
  }, [runId])

  useEffect(() => {
    let cancelled = false
    casesApi
      .getSLA(id)
      .then((res) => {
        if (cancelled) return
        const due = res.data.resolution_due
        const health = res.data.health_status
        setSla(due ? { due, health: health || '' } : null)
      })
      .catch(() => {
        if (!cancelled) setSla(null)
      })
    return () => {
      cancelled = true
    }
  }, [id])

  useEffect(() => {
    let cancelled = false
    setRecordPhase('loading')
    setRecordError(null)
    casesApi
      .getRecord(id)
      .then((res) => {
        if (cancelled) return
        setRows(res.data.rows || [])
        setRecordPhase('ready')
      })
      .catch((e) => {
        if (cancelled) return
        setRows([])
        setRecordError(detailOf(e, 'Couldn’t load the record'))
        setRecordPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [id, recordKey])

  const tabCounts = counts(fold, rows.length)
  const gaps = visibilityGaps(fold)
  const recalled = recallEntityCalls(fold)
  const shown = chip === 'all' ? rows : rows.filter((row) => recordChip(row.kind) === chip)

  const promptFor = (text?: string) => {
    const base = c
      ? `Investigate case ${c.id}: "${c.title}" — ${c.prio} priority, status ${pill}, ${c.findings} linked findings.`
      : `Investigate case ${id}.`
    return text ? `${base}\n\nWhy?\n${text}` : base
  }

  const replay = async () => {
    if (!runId) return
    setBusy(true)
    setNote('')
    try {
      const res = await workflowApi.replayRun(runId)
      const data = res.data as { decisions?: unknown[] }
      const n = Array.isArray(data.decisions) ? data.decisions.length : 0
      setNote(n ? `Replay returned ${n} decision${n === 1 ? '' : 's'}.` : 'Replay returned.')
    } catch (e) {
      setNote(detailOf(e, 'Replay failed'))
    } finally {
      setBusy(false)
    }
  }

  const verify = async () => {
    if (!runId) return
    setBusy(true)
    setNote('')
    try {
      const res = await workflowApi.verifyRun(runId)
      const data = res.data as { ok?: boolean; events?: number; reason?: string }
      setNote(data.ok ? `Chain verified (${data.events ?? 0} events).` : `Chain break${data.reason ? `: ${data.reason}` : ''}.`)
    } catch (e) {
      setNote(detailOf(e, 'Verify failed'))
    } finally {
      setBusy(false)
    }
  }

  const download = async () => {
    if (!latest) return
    setBusy(true)
    setNote('')
    try {
      const res = await orchestratorApi.exportInvestigation(latest.investigation_id)
      const blob = res.data instanceof Blob ? res.data : new Blob([JSON.stringify(res.data)])
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${latest.investigation_id}-export`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setNote(detailOf(e, 'Export failed'))
    } finally {
      setBusy(false)
    }
  }

  const decide = async (sourceId: string, act: () => Promise<unknown>) => {
    if (decisionBusyRef.current) return
    const forCase = id
    decisionBusyRef.current = sourceId
    setDecisionBusy(sourceId)
    setDecisionError(null)
    try {
      await act()
      if (seenCase.current === forCase) await loadNeeds()
    } catch (e) {
      if (seenCase.current === forCase) setDecisionError(detailOf(e, 'Could not update that decision'))
    } finally {
      if (seenCase.current === forCase && decisionBusyRef.current === sourceId) {
        decisionBusyRef.current = null
        setDecisionBusy(null)
      }
    }
  }

  const reopen = async () => {
    setBusy(true)
    setNote('')
    try {
      await casesApi.update(id, { status: 'open' })
      onChanged()
    } catch (e) {
      setNote(detailOf(e, 'Couldn’t reopen the case'))
    } finally {
      setBusy(false)
    }
  }

  const findings = fold?.kind === 'lead' ? fold.findings : []
  const hypotheses = fold?.kind === 'hunt' ? fold.hypotheses : []
  const needsBlock = (
    <CaseNeeds
      items={needsItems}
      busy={decisionBusy !== null}
      error={decisionError}
      onApprove={(sourceId) => void decide(sourceId, () => approvalsApi.approve(sourceId))}
      onReject={(sourceId, reason) => void decide(sourceId, () => approvalsApi.reject(sourceId, reason))}
    />
  )

  return (
    <div className="detail-pane">
      <div className="detail-head">
        <div className="dh-crumb">
          <button type="button" className="back" onClick={onBack}>Cases</button>
          <span aria-hidden>›</span>
          <span className="mono">Case {id}</span>
          <div className="dh-crumb-end">
            {c && <CaseMenu onEdit={onEdit} onMerge={onMerge} onDelete={canDelete ? onDelete : undefined} />}
            {onExpand && (
              <>
                <button type="button" className="btn ghost icon" aria-label="Expand" title="Expand" onClick={onExpand}>
                  <Icon name="fit" size={15} />
                </button>
                <button type="button" className="btn ghost icon" aria-label="Close" title="Close" onClick={onBack}>
                  <Icon name="close" size={15} />
                </button>
              </>
            )}
          </div>
        </div>
        {phase === 'error' ? (
          <div className="muted" style={{ padding: '6px 0' }}>Couldn’t load this case: {error}</div>
        ) : c ? (
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
            <div style={{ flex: 1 }}>
              <h2>{c.title}</h2>
              <div className="dh-meta">
                <span className={`prio ${c.prio}`}>{c.prio[0].toUpperCase()}{c.prio.slice(1)} priority</span>
                <span className={`status ${pill}`}>{pill}</span>
                <span>{latest?.workflow_id || 'No workflow'}</span>
                <span>{c.findings} alerts combined</span>
                <span>Opened {created}</span>
                <span>
                  <Icon name="clock" size={13} /> Resolve by {sla ? when(sla.due) : '—'}
                  {sla?.health ? ` · ${sla.health}` : ''}
                </span>
              </div>
              <LinkedFindings items={linkedFindings} />
              <NotMeasured className="case-trust" />
            </div>
          </div>
        ) : (
          <div className="muted" style={{ padding: '6px 0' }}>Loading case…</div>
        )}
      </div>

      <nav className="detail-tabs" role="tablist" aria-label="Case sections">
        {TABS.map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            className={`tab${tab === name ? ' active' : ''}`}
            onClick={() => setTab(name)}
          >
            {name} <span className="case-count">{tabCounts[name]}</span>
          </button>
        ))}
      </nav>
      {needsCount > 0 && tab !== 'Summary' && (
        <button type="button" className="case-needs-strip" onClick={() => setTab('Summary')}>
          Needs you
        </button>
      )}

      <div className="case-stage">
        <div className="detail-body" key={tab}>
          {tab === 'Summary' && (
            closed ? (
              <>
                {needsBlock}
                <section>
                  <h3>Verdict</h3>
                  <p>{closure?.verdict || '—'}</p>
                  <p className="muted">
                    {closure?.closure_category || '—'}
                    {closure?.closed_by ? ` · closed by ${closure.closed_by}` : ''}
                    {closure?.closed_by_kind ? ` (${closure.closed_by_kind})` : ''}
                  </p>
                </section>
                <section>
                  <h3>Strongest findings</h3>
                  <FindingList fold={fold} />
                </section>
                <div className="case-actions">
                  {runId && <button className="btn" onClick={replay} disabled={busy}>Replay</button>}
                  <button className="btn" onClick={reopen} disabled={busy}>Reopen</button>
                </div>
              </>
            ) : (
              <>
                {needsBlock}
                <section>
                  <h3>Now · phase {fold?.kind === 'hunt' ? fold.iteration : fold?.iterations ?? 0}</h3>
                  <p>
                    {foldPhase === 'loading' && 'Loading the run…'}
                    {foldPhase === 'error' && 'The run could not be read.'}
                    {foldPhase === 'ready' && (fold ? `${fold.doing || 'Nothing decided yet'}${fold.worker ? ` · ${fold.worker}` : ''}` : 'No run on this case yet.')}
                  </p>
                  {fold?.outcome && <p className="muted">Run outcome {fold.outcome}{fold.reason ? ` — ${fold.reason}` : ''}</p>}
                </section>
                <section>
                  <h3>Findings so far</h3>
                  <FindingList fold={fold} />
                </section>
                <section>
                  <h3>Later</h3>
                  <LaterRow title="What it changed" line="What this phase changed in the estate." />
                  <LaterRow title="Planned next" line="What the run intends to do next." />
                </section>
                <section>
                  <h3>Agents on this case, right now</h3>
                  {live.length === 0 && <p className="muted">No live investigation.</p>}
                  {live.map((item) => (
                    <p key={item.investigation_id}>{item.workflow_id} · {item.status}</p>
                  ))}
                </section>
                <div className="case-doors">
                  {TABS.filter((name) => name !== 'Summary').map((name) => (
                    <button key={name} className="btn ghost" onClick={() => setTab(name)}>
                      {name} <span className="case-count">{tabCounts[name]}</span>
                    </button>
                  ))}
                </div>
              </>
            )
          )}

          {tab === 'Explanations' && (
            foldPhase === 'error' ? (
              <p>The run could not be read.</p>
            ) : fold?.kind === 'hunt' ? (
              hypotheses.length === 0 ? (
                <EmptyState compact icon="search" title="No explanations yet" />
              ) : (
                <div className="table-wrap">
                  <table className="tbl">
                    <thead><tr><th>Explanation</th><th>Standing</th><th>For</th><th>Against</th></tr></thead>
                    <tbody>
                      {hypotheses.map((row) => (
                        <tr key={row.hypothesis_id}>
                          <td>
                            {row.statement || row.hypothesis_id}
                            {row.resolution_reason && (row.status === 'inconclusive' || row.status === 'parked' || row.status === 'handed_off') && (
                              <div className="muted">{row.resolution_reason}</div>
                            )}
                          </td>
                          <td>{explanationWord(row.status, row.supports, row.weakens)}</td>
                          <td>{row.supports}</td>
                          <td>{row.weakens}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            ) : (
              <p>
                {honestLine()}
                <Mark text="Hunt, root cause, and adjudicate test explanations. This run does not." />
              </p>
            )
          )}

          {tab === 'Evidence' && (
            fold?.kind === 'hunt' ? (
              fold.evidence.length === 0 ? (
                <EmptyState compact icon="shield" title="No evidence yet" />
              ) : (
                <EvidenceTable
                  focusId={focusEvidence}
                  rows={fold.evidence.map((row) => ({
                    id: row.evidence_id,
                    step: String(row.iteration),
                    observation: row.is_gap ? `${row.summary} (gap)` : row.summary,
                    source: row.source_system,
                    bears: row.bears_on.map((link) => `${link.relation} ${link.hypothesis_id}`).join(', ') || '—',
                  }))}
                />
              )
            ) : findings.length === 0 ? (
              <EmptyState compact icon="shield" title="No evidence yet" />
            ) : (
              <EvidenceTable
                rows={findings.map((row, i) => ({
                  id: `${row.agent_id}-${i}`,
                  step: String(i + 1),
                  observation: row.answer || '—',
                  source: row.agent_id,
                  bears: '—',
                }))}
              />
            )
          )}

          {tab === 'Checked' && (
            !fold || fold.calls.length === 0 ? (
              <EmptyState compact icon="search" title="No questions asked yet" />
            ) : (
              <div className="table-wrap">
                <table className="tbl">
                  <thead><tr><th>Question</th><th>Tool</th><th>Result size</th><th>Cost</th><th>Latency</th></tr></thead>
                  <tbody>
                    {fold.calls.map((call, i) => (
                      <tr key={`${call.tool}-${i}`}>
                        <td>{call.question || '—'}</td>
                        <td>{call.tool || '—'}</td>
                        <td>{call.result_length}</td>
                        <td>{money(call.cost_usd)}</td>
                        <td>{latency(call.duration_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          )}

          {tab === 'Memory and blind spots' && (
            !fold?.recall && recalled.length === 0 && gaps.length === 0 ? (
              <EmptyState compact icon="brain" title="No memory recorded" body="Recall is what the run journaled, not a fresh read." />
            ) : (
              <>
                <section>
                  <h3>Recall</h3>
                  {fold?.recall?.unavailable ? (
                    <p>Recall did not happen: {fold.recall.unavailable}{fold.recall.keys.length ? ` (${fold.recall.keys.join(', ')})` : ''}</p>
                  ) : fold?.recall ? (
                    <>
                      <p>{fold.recall.keys.length ? fold.recall.keys.join(', ') : 'No entities recalled.'}</p>
                      {fold.recall.sightings.map((row) => <p key={row}>{row}</p>)}
                      {fold.recall.verdicts.map((row) => <p key={row}>{row}</p>)}
                      {fold.recall.gaps.length > 0 && <p className="muted">Declared gaps: {fold.recall.gaps.join('; ')}</p>}
                    </>
                  ) : (
                    <p className="muted">The run did not journal an opening recall.</p>
                  )}
                  {recalled.map((call, i) => (
                    <p key={i}>recall_entity · {call.question || '—'} · {call.result_length} bytes</p>
                  ))}
                </section>
                <section>
                  <h3>Visibility gaps</h3>
                  {gaps.length === 0 && <p className="muted">None recorded.</p>}
                  {gaps.map((gap) => <p key={gap.id}>{gap.text}</p>)}
                </section>
              </>
            )
          )}

          {tab === 'Record' && (
            <>
              <p>
                Newest first.
                <Mark text={CHAINED} />
              </p>
              <div className="case-chips" role="group" aria-label="Record source">
                {(['all', 'agent', 'human', 'memory', 'system'] as const).map((name) => (
                  <button key={name} className={`btn ghost${chip === name ? ' active' : ''}`} onClick={() => setChip(name)}>
                    {name}
                  </button>
                ))}
              </div>
              {recordPhase === 'loading' && <EmptyState loading compact icon="clock" title="Loading the record…" />}
              {recordPhase === 'error' && (
                <EmptyState
                  error
                  compact
                  icon="alert"
                  title="Couldn’t load the record"
                  body={recordError || undefined}
                  primary={{ label: 'Retry', onClick: () => setRecordKey((k) => k + 1), icon: 'refresh' }}
                />
              )}
              {recordPhase === 'ready' && shown.length === 0 && (
                <EmptyState compact icon="clock" title="No record yet" />
              )}
              {recordPhase === 'ready' && shown.map((row) => (
                <div key={row.id} className="case-record">
                  <span className="tag">{recordChip(row.kind)}</span>
                  <div>
                    <div>{row.text || row.kind}</div>
                    <div className="muted">{when(row.at)}{row.chained ? ' · chained' : ''}</div>
                  </div>
                  <button type="button" className="btn ghost" onClick={() => setAskSeed({ id, text: promptFor(row.text) })}>Why?</button>
                </div>
              ))}
              <div className="case-actions">
                {runId && <button className="btn" onClick={replay} disabled={busy}>Replay</button>}
                {runId && <button className="btn" onClick={verify} disabled={busy}>Verify</button>}
                {latest && <button className="btn" onClick={download} disabled={busy}>Export</button>}
              </div>
            </>
          )}
          {note && <p className="muted">{note}</p>}
        </div>

        <aside className="case-side" aria-label="Case details">
          <div><span className="k">Workflow</span><div>{latest?.workflow_id || '—'}</div></div>
          <div>
            <span className="k">Budget</span>
            <div>
              {latest ? `${money(latest.cost_usd)} / ${money(latest.max_cost_usd)} · ${latest.budget_health}` : '—'}
            </div>
          </div>
          <div>
            <span className="k">Resolve by</span>
            <div>{sla ? `${when(sla.due)}${sla.health ? ` · ${sla.health}` : ''}` : '—'}</div>
          </div>
          <div>
            <span className="k">Entities</span>
            <div>{fold?.recall && !fold.recall.unavailable && fold.recall.keys.length ? fold.recall.keys.join(', ') : '—'}</div>
          </div>
          <div><span className="k">Cost</span><div>{money(fold?.costUsd ?? latest?.cost_usd)}</div></div>
          <details className="case-fold">
            <summary>People</summary>
            <p>Owner {c?.ownerName || '—'}</p>
            <CommentsCard caseId={id} />
            <TasksCard caseId={id} />
            <Tickets caseId={id} />
          </details>
          <details className="case-fold">
            <summary>Known about these entities</summary>
            <p className="muted">
              {fold?.recall
                ? fold.recall.unavailable
                  ? `Recall did not happen: ${fold.recall.unavailable}`
                  : `${fold.recall.keys.join(', ') || 'No entities'}${fold.recall.verdicts.length ? `. Verdicts: ${fold.recall.verdicts.join('; ')}` : ''}${fold.recall.gaps.length ? `. Gaps: ${fold.recall.gaps.join('; ')}` : ''}`
                : 'The run did not journal a recall.'}
            </p>
          </details>
          <details className="case-fold">
            <summary>Files</summary>
            <EvidenceCard caseId={id} title="Files" />
          </details>
          <details className="case-fold">
            <summary>IOCs</summary>
            <IOCsCard caseId={id} />
          </details>
        </aside>
      </div>

      <Chat
        pinned
        open
        onClose={() => undefined}
        pageKey={pageKey}
        lockedCaseId={id}
        seed={askSeed?.id === id ? askSeed.text : null}
        onSeedConsumed={() => setAskSeed(null)}
        evidenceIds={fold?.kind === 'hunt' ? fold.evidence.map((row) => row.evidence_id) : []}
        onCite={(evidenceId) => {
          setTab('Evidence')
          setFocusEvidence(evidenceId)
        }}
      />
    </div>
  )
}

function LaterRow({ title, line }: { title: string; line: string }) {
  return (
    <div className="case-later" aria-disabled="true">
      <b>{title}</b>
      <span>{line}</span>
      <Mark text={LATER} />
    </div>
  )
}

function FindingList({ fold }: { fold: RunFold | null }) {
  if (!fold) return <p className="muted">No findings yet.</p>
  if (fold.kind === 'hunt') {
    if (fold.hypotheses.length === 0) return <p className="muted">No findings yet.</p>
    const ranked = [...fold.hypotheses].sort((a, b) => b.supports - a.supports || b.weakens - a.weakens)
    return (
      <ul>
        {ranked.slice(0, 6).map((row) => (
          <li key={row.hypothesis_id}>
            {explanationWord(row.status, row.supports, row.weakens)} — {row.statement || row.hypothesis_id}
          </li>
        ))}
      </ul>
    )
  }
  if (fold.findings.length === 0) return <p className="muted">No findings yet.</p>
  return (
    <ul>
      {fold.findings.slice(0, 6).map((row, i) => (
        <li key={i}>{row.agent_id}: {row.answer || '—'}</li>
      ))}
    </ul>
  )
}

function EvidenceTable({
  rows,
  focusId,
}: {
  rows: { id: string; step: string; observation: string; source: string; bears: string }[]
  focusId?: string | null
}) {
  const focusRef = useRef<HTMLTableRowElement>(null)
  useEffect(() => {
    const node = focusRef.current
    if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' })
  }, [focusId, rows])
  return (
    <div className="table-wrap">
      <table className="tbl">
        <thead><tr><th>Step</th><th>Observation</th><th>Source</th><th>Bears on</th></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              ref={row.id === focusId ? focusRef : undefined}
              className={row.id === focusId ? 'cite-target' : undefined}
              data-evidence-id={row.id}
            >
              <td>{row.step}</td>
              <td>{row.observation || '—'}</td>
              <td>{row.source || '—'}</td>
              <td>{row.bears}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Tickets({ caseId }: { caseId: string }) {
  const [rows, setRows] = useState<{ id: string; label: string }[]>([])
  const [phase, setPhase] = useState<Phase>('loading')

  useEffect(() => {
    let cancelled = false
    casesApi
      .getEscalations(caseId)
      .then((res) => {
        if (cancelled) return
        setRows((res.data.escalations || []).map((row) => ({
          id: String(row.escalation_id ?? row.escalated_to),
          label: [row.escalated_to, row.reason].filter(Boolean).join(' — ') || 'Ticket',
        })))
        setPhase('ready')
      })
      .catch(() => {
        if (!cancelled) setPhase('error')
      })
    return () => {
      cancelled = true
    }
  }, [caseId])

  return (
    <section>
      <h3>Linked tickets</h3>
      {phase === 'loading' && <p className="muted">Loading tickets…</p>}
      {phase === 'error' && <p className="muted">Couldn’t load tickets.</p>}
      {phase === 'ready' && rows.length === 0 && <p className="muted">No linked tickets.</p>}
      {rows.map((row) => <p key={row.id}>{row.label}</p>)}
    </section>
  )
}
