/* ============================================================
   AI Decisions — A console table → B review queue on click.
   Wired to the real backend (aiDecisionsApi / approvalsApi) via
   useDecisions; the four tabs are two features: decision
   analytics + feedback, and a separate pending-approvals queue.
   See DECISIONS_WIRING.md.
   ============================================================ */
import { useEffect, useState, type ReactNode } from 'react'
import { format } from 'date-fns'
import { Icon } from '../../shared/icons'
import { Hbars, type HbarItem } from '../../shared/charts'
import { getAgentDisplayName } from '../../data/mappers'
import type { Decision, Outcome } from '../../data/appData'
import type { ScreenProps } from '../../shared/types'
import {
  useDecisions,
  usePendingDecisions,
  useDecisionStats,
  usePendingApprovals,
  type Phase,
  type DecisionStatus,
  type DecisionStats,
  type ApprovalAction,
} from './useDecisions'
import { aiDecisionsApi, approvalsApi } from '../../../services/api'
import { Popup, Field, TextInput, Select, Rating, Slider, Dropdown, activateOnKey } from '../../shared/ui'

type DecTab = 'pending' | 'all' | 'analytics' | 'approvals'
type Assessment = 'agree' | 'partial' | 'disagree'

const AGENT_IDS = ['all', 'triage', 'investigation', 'correlation', 'auto_responder', 'threat_hunter', 'orchestrator']
const AGENT_OPTS = AGENT_IDS.map((id) => ({ value: id, label: id === 'all' ? 'All agents' : getAgentDisplayName(id) }))
const STATUS_OPTS = [
  { value: 'all', label: 'All status' },
  { value: 'pending', label: 'Awaiting feedback' },
  { value: 'completed', label: 'Reviewed' },
]
const ASSESS_LABEL: Record<Assessment, string> = { agree: 'Agree', partial: 'Partially agree', disagree: 'Disagree' }
const OUTCOME_OPTS = [
  { value: 'unknown', label: 'Unknown — unable to determine yet' },
  { value: 'true_positive', label: 'True positive — real threat, correctly identified' },
  { value: 'false_positive', label: 'False positive — not a threat' },
  { value: 'true_negative', label: 'True negative — correctly dismissed' },
  { value: 'false_negative', label: 'False negative — missed a real threat' },
]

/** prefer FastAPI's `detail`, fall back to the axios message, then a default */
function apiErr(e: unknown, fallback: string): string {
  const r = e as { response?: { data?: { detail?: string } }; message?: string }
  return r?.response?.data?.detail || r?.message || fallback
}

function prettyLabel(k: string): string {
  return k.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/** outcome → severity bar colour for the analytics distribution */
function outcomeBarCls(k: string): string {
  if (k === 'true_positive' || k === 'true_negative') return 'c-ok'
  if (k === 'false_positive') return 'c-crit'
  if (k === 'false_negative') return 'c-high'
  return ''
}

function fmtTime(iso?: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : format(d, 'MMM d, HH:mm')
}

function confMeter(pct: number) {
  const cls = pct >= 85 ? 'hi' : pct < 70 ? 'lo' : ''
  return (
    <span className="confmeter">
      <span className="track">
        <i className={cls} style={{ width: `${pct}%` }} />
      </span>
      <span className="num">{pct}%</span>
    </span>
  )
}

function outcomeChip(o: Outcome) {
  const map: Record<Outcome, [string, string]> = {
    agree: ['agree', 'Agreed'],
    disagree: ['disagree', 'Rejected'],
    modify: ['modify', 'Modified'],
    pending: ['pending', 'Pending'],
  }
  const [c, t] = map[o] || ['pending', o]
  return <span className={`outcome ${c}`}>{t}</span>
}

/* ---------------- shared state rows ---------------- */
function StateRow({ cols, children }: { cols: number; children: ReactNode }) {
  return (
    <tr>
      <td colSpan={cols} className="muted" style={{ textAlign: 'center', padding: '40px 0' }}>
        {children}
      </td>
    </tr>
  )
}

function RetryState({ msg, reload }: { msg: string | null; reload: () => void }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10 }}>
      <span>{msg}</span>
      <button className="btn ghost" onClick={reload}>Retry</button>
    </div>
  )
}

/* ---------------- KPI strip ---------------- */
function DecKpis({ s }: { s: DecisionStats | null }) {
  const days = s ? `last ${s.period_days} days` : 'last 30 days'
  return (
    <div className="kpi-strip">
      <div className="kpi">
        <div className="k-label">Total Decisions</div>
        <div className="k-row"><span className="k-val">{s ? s.total_decisions : '—'}</span></div>
        <div className="k-note">{days}</div>
      </div>
      <div className="kpi">
        <div className="k-label">Feedback Rate</div>
        <div className="k-row"><span className="k-val">{s ? `${Math.round(s.feedback_rate * 100)}%` : '—'}</span></div>
        <div className="k-note">{s ? `${s.total_with_feedback} reviewed` : '—'}</div>
      </div>
      <div className="kpi">
        <div className="k-label">Agreement Rate</div>
        <div className="k-row"><span className="k-val" style={{ color: 'var(--ok)' }}>{s ? `${Math.round(s.agreement_rate * 100)}%` : '—'}</span></div>
        <div className="k-note">model accuracy</div>
      </div>
      <div className="kpi">
        <div className="k-label">Time Saved</div>
        <div className="k-row"><span className="k-val high">{s ? `${Math.round(s.total_time_saved_hours)}h` : '—'}</span></div>
        <div className="k-note">{days}</div>
      </div>
    </div>
  )
}

/* ---------------- decisions table (Pending + All tabs) ---------------- */
const DECISIONS_PAGE = 25

function DecisionRows({
  rows,
  phase,
  error,
  reload,
  onSelect,
  empty,
}: {
  rows: Decision[]
  phase: Phase
  error: string | null
  reload: () => void
  onSelect: (id: string) => void
  empty: string
}) {
  // Cap visible rows so the table doesn't scroll endlessly; "Show more" reveals
  // another page. Reset to the first page whenever the row set changes
  // (tab switch / filter / search) — keyed on a stable signature, not the
  // array identity, so it doesn't reset on every render.
  const [visible, setVisible] = useState(DECISIONS_PAGE)
  const sig = `${rows.length}:${rows[0]?.id ?? ''}:${rows[rows.length - 1]?.id ?? ''}`
  useEffect(() => { setVisible(DECISIONS_PAGE) }, [sig])

  const shown = rows.slice(0, visible)
  const more = rows.length - shown.length

  return (
    <div className="table-wrap list-scroll">
      <table className="tbl decisions-tbl">
        <thead>
          <tr>
            <th>Agent</th><th>Type</th><th>Investigation</th><th>Confidence</th>
            <th>AI Decision</th><th>Outcome</th><th>Time Saved</th><th>Time</th><th />
          </tr>
        </thead>
        <tbody>
          {phase === 'loading' && <StateRow cols={9}>Loading decisions…</StateRow>}
          {phase === 'error' && <StateRow cols={9}><RetryState msg={error} reload={reload} /></StateRow>}
          {phase === 'ready' && rows.length === 0 && <StateRow cols={9}>{empty}</StateRow>}
          {phase === 'ready' &&
            shown.map((d) => (
              <tr key={d.id} className="clickable decisions-tbl-row" onClick={() => onSelect(d.id)}>
                <td><span className="chip" style={{ borderColor: 'var(--accent-line)', color: 'var(--accent-2)' }}>{d.agent}</span></td>
                <td>{d.type}</td>
                <td><span className="id-cell">{d.inv}</span></td>
                <td>{confMeter(d.conf)}</td>
                <td className="muted">{d.ai}</td>
                <td>{outcomeChip(d.outcome)}</td>
                <td><span className="mono">{d.saved}</span></td>
                <td className="muted">{d.time}</td>
                <td>
                  <span className="row-act">
                    <button title="Review" onClick={(e) => { e.stopPropagation(); onSelect(d.id) }}><Icon name="eye" /></button>
                  </span>
                </td>
              </tr>
            ))}
          {phase === 'ready' && more > 0 && (
            <tr className="decisions-more-row">
              <td colSpan={9} style={{ textAlign: 'center', padding: '12px 0' }}>
                <span className="muted" style={{ marginRight: 12 }}>
                  Showing {shown.length} of {rows.length}
                </span>
                <button className="btn ghost" onClick={() => setVisible((v) => v + DECISIONS_PAGE)}>
                  Show {Math.min(DECISIONS_PAGE, more)} more
                </button>
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

/* ---------------- Analytics tab ---------------- */
function AnalyticsTab({ s, phase, error, reload }: { s: DecisionStats | null; phase: Phase; error: string | null; reload: () => void }) {
  if (phase === 'loading') return <div className="muted" style={{ padding: '40px 22px', textAlign: 'center' }}>Loading analytics…</div>
  if (phase === 'error') {
    return (
      <div style={{ padding: '40px 22px', textAlign: 'center' }} className="muted">
        <RetryState msg={`Couldn’t load analytics: ${error}`} reload={reload} />
      </div>
    )
  }
  if (!s) return <div className="muted" style={{ padding: '40px 22px', textAlign: 'center' }}>No analytics available.</div>

  const total = s.total_with_feedback || 0
  const items: HbarItem[] = Object.entries(s.outcomes || {}).map(([k, v]) => ({
    label: prettyLabel(k),
    val: v,
    pct: total > 0 ? Math.round((v / total) * 100) : 0,
    cls: outcomeBarCls(k),
  }))
  const perDay = s.period_days > 0 ? Math.round(s.total_decisions / s.period_days) : 0
  const avgSaved = s.total_decisions > 0 ? Math.round(s.total_time_saved_minutes / s.total_decisions) : 0
  const needReview = Math.max(0, s.total_decisions - s.total_with_feedback)

  return (
    <div style={{ padding: 22 }} className="grid grid-cols-2 gap-4">
      <section className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden">
        <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft"><h3 className="text-[14.5px]">Outcome distribution</h3></div>
        <div className="p-[18px]">{items.length > 0 ? <Hbars items={items} /> : <div className="muted">No outcome data yet.</div>}</div>
      </section>
      <section className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden">
        <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft"><h3 className="text-[14.5px]">Performance metrics</h3></div>
        <div className="p-[18px]">
          <div className="kv">
            <div className="row"><span className="k">Decisions per day</span><span className="val">{perDay}</span></div>
            <div className="row"><span className="k">Avg time saved / decision</span><span className="val">{avgSaved}m</span></div>
            <div className="row"><span className="k">Decisions needing review</span><span className="val">{needReview}</span></div>
            <div className="row"><span className="k">Avg accuracy grade</span><span className="val">{s.avg_accuracy_grade ? `${Math.round(s.avg_accuracy_grade * 100)}%` : '—'}</span></div>
          </div>
        </div>
      </section>
    </div>
  )
}

/* ---------------- Pending Approvals tab ---------------- */
function ApprovalsTab({
  actions,
  phase,
  error,
  reload,
  onApprove,
  onReject,
  acting,
  banner,
}: {
  actions: ApprovalAction[]
  phase: Phase
  error: string | null
  reload: () => void
  onApprove: (a: ApprovalAction) => void
  onReject: (a: ApprovalAction) => void
  acting: string | null
  banner: string | null
}) {
  return (
    <>
      {banner && <div style={{ padding: '10px 22px', color: 'var(--crit)', fontSize: 13 }}>{banner}</div>}
      <div className="table-wrap list-scroll">
        <table className="tbl">
          <thead>
            <tr>
              <th>Action</th><th>Target / Run</th><th>Phase</th><th>Reason</th><th>Created</th>
              <th style={{ textAlign: 'right' }}>Decision</th>
            </tr>
          </thead>
          <tbody>
            {phase === 'loading' && <StateRow cols={6}>Loading approvals…</StateRow>}
            {phase === 'error' && <StateRow cols={6}><RetryState msg={error} reload={reload} /></StateRow>}
            {phase === 'ready' && actions.length === 0 && (
              <StateRow cols={6}>No pending approvals. Workflow phases that require approval will appear here.</StateRow>
            )}
            {phase === 'ready' &&
              actions.map((a) => (
                <tr key={a.action_id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{a.title || a.action_id}</div>
                    {a.description && <div className="muted" style={{ maxWidth: 320, fontSize: 12 }}>{a.description}</div>}
                  </td>
                  <td><span className="mono" style={{ fontSize: 11 }}>{a.workflow_run_id || a.target || '—'}</span></td>
                  <td>{a.workflow_phase_id ? <span className="chip">{a.workflow_phase_id}</span> : <span className="muted">—</span>}</td>
                  <td className="muted" title={a.reason} style={{ maxWidth: 220, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{a.reason || '—'}</td>
                  <td className="muted">{fmtTime(a.created_at)}</td>
                  <td>
                    <span style={{ display: 'inline-flex', gap: 8, justifyContent: 'flex-end', width: '100%' }}>
                      <button className="btn primary" disabled={acting === a.action_id} onClick={() => onApprove(a)}><Icon name="check2" /> Approve</button>
                      <button className="btn danger" disabled={acting === a.action_id} onClick={() => onReject(a)}><Icon name="x2" /> Reject</button>
                    </span>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

/* ---------------- detailed-feedback modal (Hybrid path) ---------------- */
function FeedbackPopup({
  open,
  decision,
  reviewer,
  onClose,
  onSubmitted,
}: {
  open: boolean
  decision: Decision
  reviewer: string
  onClose: () => void
  onSubmitted: () => void
}) {
  const [name, setName] = useState(reviewer)
  const [assessment, setAssessment] = useState<Assessment>('agree')
  const [accuracy, setAccuracy] = useState(3)
  const [reasoning, setReasoning] = useState(3)
  const [action, setAction] = useState(3)
  const [outcome, setOutcome] = useState('unknown')
  const [timeSaved, setTimeSaved] = useState(0)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => { if (open) setName(reviewer) }, [open, reviewer])

  const submit = async () => {
    if (!name.trim()) { setErr('Enter your name to submit feedback.'); return }
    setBusy(true)
    setErr('')
    try {
      await aiDecisionsApi.submitFeedback(decision.id, {
        human_reviewer: name.trim(),
        human_decision: assessment,
        accuracy_grade: accuracy / 5,
        reasoning_grade: reasoning / 5,
        action_appropriateness: action / 5,
        actual_outcome: outcome !== 'unknown' ? outcome : undefined,
        time_saved_minutes: timeSaved > 0 ? timeSaved : undefined,
        feedback_comment: comment.trim() || undefined,
      })
      onSubmitted()
    } catch (e) {
      setErr(apiErr(e, 'Failed to submit feedback'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={onClose} title="Detailed feedback" width={620}>
      <div className="flex flex-col gap-3.5">
        <Field label="Your name / analyst ID">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. analyst_jones" autoFocus />
        </Field>
        <Field label="Overall assessment">
          <div className="tabs">
            {(Object.keys(ASSESS_LABEL) as Assessment[]).map((a) => (
              <button key={a} className={`tab ${assessment === a ? 'active' : ''}`} onClick={() => setAssessment(a)}>{ASSESS_LABEL[a]}</button>
            ))}
          </div>
        </Field>
        <Field label="AI accuracy"><Rating value={accuracy} onChange={setAccuracy} /></Field>
        <Field label="Reasoning quality"><Rating value={reasoning} onChange={setReasoning} /></Field>
        <Field label="Action appropriateness"><Rating value={action} onChange={setAction} /></Field>
        <Field label="Actual outcome"><Select value={outcome} onSelect={setOutcome} options={OUTCOME_OPTS} /></Field>
        <Field label="Time saved"><Slider value={timeSaved} onChange={setTimeSaved} min={0} max={120} step={5} format={(v) => `${v} min`} /></Field>
        <Field label="Comments">
          <textarea className="feedback-box" value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Any additional feedback to help the AI learn…" />
        </Field>
        {err && <div className="text-[13px]" style={{ color: 'var(--crit)' }}>{err}</div>}
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={submit} disabled={busy}>{busy ? 'Submitting…' : 'Submit feedback'}</button>
        </div>
      </div>
    </Popup>
  )
}

/* ---------------- reject-action modal (mandatory reason) ---------------- */
function RejectActionPopup({
  open,
  action,
  busy,
  onClose,
  onConfirm,
}: {
  open: boolean
  action: ApprovalAction | null
  busy: boolean
  onClose: () => void
  onConfirm: (reason: string) => void
}) {
  const [reason, setReason] = useState('')
  useEffect(() => { if (open) setReason('') }, [open])

  return (
    <Popup open={open} onClose={onClose} title="Reject action" width={520}>
      <div className="flex flex-col gap-3.5">
        <p className="text-[13px] text-tx-2 m-0">{action?.title || action?.action_id}</p>
        <Field label="Rejection reason" hint="Required. Recorded on the workflow run’s audit trail.">
          <textarea className="feedback-box" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus placeholder="Why is this action being rejected?" />
        </Field>
        <div className="flex justify-end gap-2.5">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn danger" disabled={!reason.trim() || busy} onClick={() => onConfirm(reason.trim())}>{busy ? 'Rejecting…' : 'Reject'}</button>
        </div>
      </div>
    </Popup>
  )
}

/* ---------------- master-detail review queue ---------------- */
function DecisionsDetail({
  id,
  decisions,
  onSelect,
  onBack,
  onSubmitted,
}: {
  id: string
  decisions: Decision[]
  onSelect: (id: string) => void
  onBack: () => void
  onSubmitted: () => void
}) {
  const d = decisions.find((x) => x.id === id)
  const [reviewer, setReviewer] = useState('')
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [detailOpen, setDetailOpen] = useState(false)

  if (!d) {
    return (
      <div className="detail-pane" style={{ padding: 22 }}>
        <button className="back" onClick={onBack}><Icon name="chevL" size={13} /> All decisions</button>
        <div className="muted" style={{ marginTop: 16 }}>This decision is no longer in the current list (it may have just been reviewed).</div>
      </div>
    )
  }

  const submit = async (assessment: Assessment) => {
    if (!reviewer.trim()) { setErr('Enter your name to submit feedback.'); return }
    setBusy(true)
    setErr('')
    try {
      await aiDecisionsApi.submitFeedback(d.id, {
        human_reviewer: reviewer.trim(),
        human_decision: assessment,
        feedback_comment: comment.trim() || undefined,
      })
      onSubmitted()
    } catch (e) {
      setErr(apiErr(e, 'Failed to submit feedback'))
      setBusy(false)
    }
  }

  return (
    <div className="split">
      <div className="list-pane">
        <div className="flex items-center gap-3 flex-wrap px-[22px] py-[13px] border-b border-line" style={{ gap: 8 }}>
          <span className="text-[11px] font-semibold tracking-[0.07em] uppercase text-tx-3">Decisions ({decisions.length})</span>
        </div>
        <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
          {decisions.map((x) => (
            <div
              key={x.id}
              role="button"
              tabIndex={0}
              aria-label={`Open decision ${x.id}`}
              className={`case-row ${x.id === id ? 'sel' : ''}`}
              onClick={() => onSelect(x.id)}
              onKeyDown={activateOnKey(() => onSelect(x.id))}
            >
              <div className="cr-top">
                <span className="chip" style={{ borderColor: 'var(--accent-line)', color: 'var(--accent-2)', fontSize: 11 }}>{x.agent}</span>
                <span style={{ marginLeft: 'auto' }}>{outcomeChip(x.outcome)}</span>
              </div>
              <div className="cr-meta">
                <span>{x.type}</span>
                <span className="mono" style={{ marginLeft: 'auto' }}>{x.conf}%</span>
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="detail-pane">
        <div className="detail-head">
          <div className="dh-crumb">
            <button className="back" onClick={onBack}><Icon name="chevL" size={13} /> All decisions</button>
            <span>/</span><span className="mono">{d.id}</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
            <div style={{ flex: 1 }}>
              <h2>{d.type}: {d.ai}</h2>
              <div className="dh-meta">
                <span className="chip" style={{ borderColor: 'var(--accent-line)', color: 'var(--accent-2)' }}>{d.agent}</span>
                {outcomeChip(d.outcome)}
                <span><Icon name="clock" size={13} /> {d.time}</span>
                <span className="id-cell">{d.inv}</span>
              </div>
            </div>
          </div>
        </div>
        <div className="detail-body">
          <div className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden span-2">
            <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft"><h3 className="text-[14.5px]">AI recommendation</h3><span className="flex-1" />{confMeter(d.conf)}</div>
            <div className="p-[18px]"><p style={{ margin: '0 0 4px', color: 'var(--tx-2)', fontSize: '13.5px', lineHeight: 1.6 }}>{d.rationale || 'No rationale provided.'}</p></div>
          </div>
          {d.evidence.length > 0 && (
            <div className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden">
              <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft"><h3 className="text-[14.5px]">Supporting evidence</h3></div>
              <div className="p-[18px]">
                <div className="ev-list">
                  {d.evidence.map((e, i) => (
                    <div className="ev-item" key={i}><span className="ed" />{e}</div>
                  ))}
                </div>
              </div>
            </div>
          )}
          <div className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden">
            <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft"><h3 className="text-[14.5px]">Decision details</h3></div>
            <div className="p-[18px]">
              <div className="kv">
                <div className="row"><span className="k">AI confidence</span><span className="val">{d.conf}%</span></div>
                <div className="row"><span className="k">Est. time saved</span><span className="val">{d.saved}</span></div>
                <div className="row"><span className="k">Agent</span><span className="val">{d.agent}</span></div>
                <div className="row"><span className="k">Current outcome</span><span className="val">{outcomeChip(d.outcome)}</span></div>
              </div>
            </div>
          </div>
          <div className="bg-panel border border-line rounded-lg shadow-panel overflow-hidden span-2">
            <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft"><h3 className="text-[14.5px]">Your review</h3></div>
            <div className="p-[18px]">
              <TextInput value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Your name / analyst ID" style={{ marginBottom: 12 }} />
              <div className="flex gap-2.5 mt-1 mb-3.5">
                <button className="btn primary flex-1 justify-center" disabled={busy} onClick={() => submit('agree')}><Icon name="check2" /> Approve</button>
                <button className="btn flex-1 justify-center" disabled={busy} onClick={() => submit('partial')}><Icon name="edit" /> Modify</button>
                <button className="btn danger flex-1 justify-center" disabled={busy} onClick={() => submit('disagree')}><Icon name="x2" /> Reject</button>
              </div>
              <textarea className="feedback-box" placeholder="Add feedback to train the model (optional)…" value={comment} onChange={(e) => setComment(e.target.value)} />
              <div className="flex items-center gap-3" style={{ marginTop: 10 }}>
                <button className="btn ghost" onClick={() => setDetailOpen(true)}><Icon name="plus" /> Add detailed feedback</button>
                {busy && <span className="muted">Submitting…</span>}
                {err && <span style={{ color: 'var(--crit)', fontSize: 13 }}>{err}</span>}
              </div>
            </div>
          </div>
        </div>
      </div>

      <FeedbackPopup open={detailOpen} decision={d} reviewer={reviewer} onClose={() => setDetailOpen(false)} onSubmitted={onSubmitted} />
    </div>
  )
}

/* ---------------- screen shell ---------------- */
export default function DecisionsScreen({ setViewFull }: ScreenProps) {
  const [selected, setSelected] = useState<string | null>(null)
  const [tab, setTab] = useState<DecTab>('pending')
  const [query, setQuery] = useState('')
  const [agentF, setAgentF] = useState('all')
  const [statusF, setStatusF] = useState<DecisionStatus>('all')

  // approvals action state
  const [acting, setActing] = useState<string | null>(null)
  const [rejectFor, setRejectFor] = useState<ApprovalAction | null>(null)
  const [approvalBanner, setApprovalBanner] = useState<string | null>(null)

  const stats = useDecisionStats('all')
  const pending = usePendingDecisions()
  const all = useDecisions(agentF, statusF)
  const approvals = usePendingApprovals()

  useEffect(() => {
    setViewFull(selected !== null)
  }, [selected, setViewFull])

  const reloadDecisions = () => { pending.reload(); all.reload(); stats.reload() }
  const onSubmitted = () => { reloadDecisions(); setSelected(null) }

  const approve = async (a: ApprovalAction) => {
    setActing(a.action_id)
    setApprovalBanner(null)
    try {
      await approvalsApi.approve(a.action_id)
      approvals.reload()
    } catch (e) {
      setApprovalBanner(apiErr(e, 'Failed to approve action'))
    } finally {
      setActing(null)
    }
  }

  const confirmReject = async (reason: string) => {
    const a = rejectFor
    if (!a) return
    setActing(a.action_id)
    setApprovalBanner(null)
    try {
      await approvalsApi.reject(a.action_id, reason)
      setRejectFor(null)
      approvals.reload()
    } catch (e) {
      setApprovalBanner(apiErr(e, 'Failed to reject action'))
    } finally {
      setActing(null)
    }
  }

  const filterRows = (rows: Decision[]) => {
    const q = query.trim().toLowerCase()
    if (!q) return rows
    return rows.filter((d) => `${d.id} ${d.agent} ${d.type} ${d.inv} ${d.ai}`.toLowerCase().includes(q))
  }

  if (selected) {
    return (
      <DecisionsDetail
        id={selected}
        decisions={tab === 'pending' ? pending.rows : all.rows}
        onSelect={setSelected}
        onBack={() => setSelected(null)}
        onSubmitted={onSubmitted}
      />
    )
  }

  const tabs: [DecTab, string, number | null][] = [
    ['pending', 'Pending', pending.rows.length],
    ['all', 'All Decisions', all.rows.length],
    ['analytics', 'Analytics', null],
    ['approvals', 'Pending Approvals', approvals.actions.length],
  ]

  return (
    <>
      <DecKpis s={stats.stats} />
      <div className="bar-row" style={{ borderBottom: 0, paddingBottom: 4 }}>
        <div className="tabs" role="tablist" aria-label="Decision views">
          {tabs.map(([k, label, n]) => (
            <button
              key={k}
              role="tab"
              aria-selected={k === tab}
              className={`tab ${k === tab ? 'active' : ''}`}
              onClick={() => setTab(k)}
            >
              {label}{n != null ? ` (${n})` : ''}
            </button>
          ))}
        </div>
      </div>

      {(tab === 'pending' || tab === 'all') && (
        <div className="bar-row" style={{ paddingTop: 8 }}>
          {tab === 'all' && (
            <>
              <Dropdown label="Agent" value={agentF} options={AGENT_OPTS} onSelect={setAgentF} selected={agentF !== 'all'} onClear={() => setAgentF('all')} />
              <Dropdown label="Status" value={statusF} options={STATUS_OPTS} onSelect={(v) => setStatusF(v as DecisionStatus)} selected={statusF !== 'all'} onClear={() => setStatusF('all')} />
            </>
          )}
          <div className="search" style={{ marginLeft: 'auto', maxWidth: 280 }}>
            <span><Icon name="search" /></span>
            <input aria-label="Search decisions" placeholder="Search decisions…" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
        </div>
      )}

      {tab === 'pending' && (
        <DecisionRows rows={filterRows(pending.rows)} phase={pending.phase} error={pending.error} reload={pending.reload} onSelect={setSelected} empty="No decisions awaiting feedback." />
      )}
      {tab === 'all' && (
        <DecisionRows rows={filterRows(all.rows)} phase={all.phase} error={all.error} reload={all.reload} onSelect={setSelected} empty="No decisions match your filters." />
      )}
      {tab === 'analytics' && <AnalyticsTab s={stats.stats} phase={stats.phase} error={stats.error} reload={stats.reload} />}
      {tab === 'approvals' && (
        <ApprovalsTab
          actions={approvals.actions}
          phase={approvals.phase}
          error={approvals.error}
          reload={approvals.reload}
          onApprove={approve}
          onReject={setRejectFor}
          acting={acting}
          banner={approvalBanner}
        />
      )}

      <RejectActionPopup
        open={rejectFor !== null}
        action={rejectFor}
        busy={acting !== null}
        onClose={() => setRejectFor(null)}
        onConfirm={confirmReject}
      />
    </>
  )
}
