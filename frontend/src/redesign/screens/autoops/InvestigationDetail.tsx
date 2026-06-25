/* ============================================================
   Full-bleed detail for one autonomous investigation. Tabs:
   Overview (facts, summary, review notes, proposed actions, human
   review, recent activity) · Files · Reasoning trace (GH #79) ·
   Chain of custody (GH #192). Ported from the legacy Orchestrator
   detail dialog, restyled to the redesign tokens.
   ============================================================ */
import { useEffect, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Icon } from '../../shared/icons'
import { Markdown } from '../../shared/Markdown'
import { orchestratorApi } from '../../../services/api'
import { useInvestigationDetail, type InvestigationDetailData } from './useInvestigationDetail'
import { StatusBadge } from './statusBadge'

interface Props {
  id: string
  onBack: () => void
  openChat: (prompt?: string) => void
  busy: string | null
  wake: (id: string) => unknown
  killInvestigation: (id: string) => unknown
  review: (id: string, action: 'approve' | 'rework', notes?: string) => unknown
}

type TabKey = 'overview' | 'files' | 'reasoning' | 'custody'

export default function InvestigationDetail({ id, onBack, openChat, busy, wake, killInvestigation, review }: Props) {
  const navigate = useNavigate()
  const { detail, reasoning, coc, phase, error, reload } = useInvestigationDetail(id)
  const [tab, setTab] = useState<TabKey>('overview')

  const runAndReload = async (p: unknown) => {
    await Promise.resolve(p)
    reload()
  }

  const handleExport = async () => {
    const res = await orchestratorApi.exportInvestigation(id)
    const blob = new Blob([res.data as BlobPart], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `inv-${id}-chain-of-custody.json`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  const files = detail?.files || []
  const tabs: [TabKey, string][] = [
    ['overview', 'Overview'],
    ['files', `Files${files.length ? ` · ${files.length}` : ''}`],
    ['reasoning', `Reasoning${reasoning.length ? ` · ${reasoning.length}` : ''}`],
    ['custody', 'Chain of custody'],
  ]

  const canWake = detail && ['sleeping', 'needs_rework', 'failed', 'completed'].includes(detail.status)
  const canKill = detail && ['executing', 'assigned'].includes(detail.status)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* header */}
      <div className="flex items-center gap-3 px-[22px] py-[13px] border-b border-line">
        <button className="btn ghost icon" onClick={onBack} title="Back to queue"><Icon name="chevL" /></button>
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <h2 className="text-[15px] font-semibold tracking-[-0.01em]">Investigation</h2>
            <span className="id-cell">{id}</span>
            {detail && <StatusBadge status={detail.status} />}
          </div>
          <p className="text-xs text-tx-3 mt-0.5 truncate">
            {detail ? `${detail.skill_id} · ${detail.iteration_count} iterations · $${Number(detail.cost_usd || 0).toFixed(4)}` : 'Loading…'}
          </p>
        </div>
        <span className="grow" />
        {canWake && (
          <button className="btn ghost" disabled={busy === `wake:${id}`} onClick={() => runAndReload(wake(id))}>
            <Icon name="play" /> Restart
          </button>
        )}
        {canKill && (
          <button className="btn danger" disabled={busy === `kill:${id}`} onClick={() => runAndReload(killInvestigation(id))}>
            <Icon name="x2" /> Kill
          </button>
        )}
        <button className="btn ghost" onClick={handleExport}><Icon name="download" /> Export</button>
        <button className="btn ghost" title="View AI decisions" onClick={() => navigate('/decisions')}>
          <Icon name="brain" /> AI Decisions
        </button>
        <button
          className="btn primary"
          onClick={() => openChat(`Review autonomous investigation ${id}${detail ? ` (${detail.skill_id})` : ''}. Summarize the findings and recommend next steps.`)}
        >
          <Icon name="sparkle" /> Ask Vigil
        </button>
      </div>

      {/* tabs */}
      <div className="tabs px-[22px] pt-3 border-b border-line">
        {tabs.map(([key, label]) => (
          <button key={key} className={`tab${tab === key ? ' active' : ''}`} onClick={() => setTab(key)}>{label}</button>
        ))}
      </div>

      {/* body */}
      <div style={{ flex: 1, overflow: 'auto' }} className="px-[22px] py-5">
        {phase === 'loading' && <div className="text-sm text-tx-3 py-16 text-center">Loading investigation…</div>}
        {phase === 'error' && (
          <div className="py-16 text-center flex flex-col items-center gap-2.5">
            <span className="text-sm text-tx-3">Couldn’t load investigation: {error}</span>
            <button className="btn ghost" onClick={reload}>Retry</button>
          </div>
        )}
        {phase === 'ready' && detail && (
          <>
            {tab === 'overview' && (
              <OverviewTab detail={detail} busy={busy} onReview={(action, notes) => runAndReload(review(id, action, notes))} />
            )}
            {tab === 'files' && <FilesTab id={id} files={files} />}
            {tab === 'reasoning' && <ReasoningTab interactions={reasoning as unknown as Interaction[]} />}
            {tab === 'custody' && <CustodyTab coc={coc as unknown as Coc | null} />}
          </>
        )}
      </div>
    </div>
  )
}

/* ---------------- Overview ---------------- */
function OverviewTab({
  detail, busy, onReview,
}: {
  detail: InvestigationDetailData
  busy: string | null
  onReview: (action: 'approve' | 'rework', notes?: string) => void
}) {
  const [notes, setNotes] = useState('')
  const reviewing = busy === `review:${detail.investigation_id}`
  const actions = detail.proposed_actions || []
  const log = detail.recent_log || []

  return (
    <div className="flex flex-col gap-4 max-w-[920px]">
      {detail.summary && <Callout tone="info" title="Summary" markdown><Markdown>{detail.summary}</Markdown></Callout>}
      {detail.master_review_notes && <Callout tone="warn" title="Master review notes" markdown><Markdown>{detail.master_review_notes}</Markdown></Callout>}
      {detail.last_error && <Callout tone="error" title="Last error">{detail.last_error}</Callout>}

      {actions.length > 0 && (
        <div className="card card-sq">
          <div className="card-h"><h3 className="text-[14px]">Proposed actions</h3></div>
          <div className="table-wrap">
            <table className="tbl">
              <thead><tr><th>Action</th><th>Target</th><th>Reason</th></tr></thead>
              <tbody>
                {actions.map((a, i) => (
                  <tr key={i}>
                    <td><span className="tag">{a.action || a.type || 'action'}</span></td>
                    <td><span className="mono">{a.target || a.entity || '—'}</span></td>
                    <td className="muted">{a.reason || a.description || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {detail.status === 'review_submitted' && (
        <div className="card card-sq">
          <div className="card-h"><h3 className="text-[14px]">Human review</h3></div>
          <div className="card-b flex flex-col gap-3">
            <textarea
              className="field-input"
              rows={3}
              placeholder="Review notes (optional)"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              style={{ resize: 'vertical', minHeight: 64, width: '100%' }}
            />
            <div className="flex gap-2.5">
              <button className="btn primary" disabled={reviewing} onClick={() => onReview('approve', notes || undefined)}>
                <Icon name="check2" /> Approve
              </button>
              <button className="btn ghost" disabled={reviewing} onClick={() => onReview('rework', notes || undefined)}>
                <Icon name="refresh" /> Request rework
              </button>
            </div>
          </div>
        </div>
      )}

      {log.length > 0 && (
        <div className="card card-sq">
          <div className="card-h"><h3 className="text-[14px]">Recent activity</h3></div>
          <div className="card-b" style={{ maxHeight: 260, overflow: 'auto' }}>
            <div className="flex flex-col gap-1 font-[var(--mono)] text-[12px]">
              {log.map((e, i) => (
                <div key={i} className="mono" style={{ lineHeight: 1.7 }}>
                  <span style={{ color: 'var(--tx-faint)' }}>{e.ts?.split('T')[1]?.split('.')[0] || ''}</span>{' '}
                  <span style={{ color: e.event === 'error' ? 'var(--crit)' : 'var(--ok)' }}>{e.event}</span>{' '}
                  {e.iteration ? <span className="text-tx-3">iter={e.iteration} </span> : null}
                  {e.cost_usd ? <span className="text-tx-3">${e.cost_usd} </span> : null}
                  <span className="text-tx-2">{e.reason || ''}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {!detail.summary && !actions.length && detail.status !== 'review_submitted' && !log.length && !detail.last_error && (
        <div className="text-sm text-tx-3 py-10 text-center">No overview details recorded yet.</div>
      )}
    </div>
  )
}

/* ---------------- Files ---------------- */
function FilesTab({ id, files }: { id: string; files: string[] }) {
  const [active, setActive] = useState<string | null>(files[0] || null)
  const [contents, setContents] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!active || contents[active] !== undefined) return
    let cancelled = false
    setLoading(true)
    orchestratorApi
      .getInvestigationFile(id, active)
      .then((r) => { if (!cancelled) setContents((p) => ({ ...p, [active]: r.data?.content || '(empty)' })) })
      .catch(() => { if (!cancelled) setContents((p) => ({ ...p, [active]: '(failed to load)' })) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [active, id, contents])

  if (!files.length) return <div className="text-sm text-tx-3 py-10 text-center">No files produced by this investigation.</div>

  return (
    <div className="card card-sq">
      <div className="tabs px-3 pt-2 border-b border-line" style={{ flexWrap: 'wrap' }}>
        {files.map((f) => (
          <button key={f} className={`tab${f === active ? ' active' : ''}`} onClick={() => setActive(f)} style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>{f}</button>
        ))}
      </div>
      <div className="card-b" style={{ maxHeight: 460, overflow: 'auto' }}>
        {loading ? (
          <div className="text-sm text-tx-3 py-6 text-center">Loading file…</div>
        ) : active && contents[active] !== undefined ? (
          <FileBody name={active} content={contents[active]} />
        ) : (
          <div className="text-sm text-tx-3 py-6 text-center">Select a file to view its content.</div>
        )}
      </div>
    </div>
  )
}

/* ---------------- Reasoning trace ---------------- */
interface Interaction {
  interaction_id?: string
  created_at?: string
  stop_reason?: string
  thinking_content?: string
  response_content?: string
  tool_calls?: { name?: string; input?: unknown }[]
  tool_results?: { tool_use_id?: string; is_error?: boolean; content?: unknown }[]
  input_tokens?: number
  output_tokens?: number
  cost_usd?: number
}

function ReasoningTab({ interactions }: { interactions: Interaction[] }) {
  const [expanded, setExpanded] = useState<string | null>(null)
  if (!interactions.length) return <div className="text-sm text-tx-3 py-10 text-center">No reasoning traces recorded for this investigation.</div>

  return (
    <div className="card card-sq" style={{ overflow: 'hidden' }}>
      {interactions.map((it, idx) => {
        const key = it.interaction_id || String(idx)
        const open = expanded === key
        return (
          <div key={key} style={{ borderBottom: idx < interactions.length - 1 ? '1px solid var(--line-soft)' : 'none' }}>
            <button
              className="flex items-center gap-3 w-full text-left px-3.5 py-2.5"
              style={{ background: 'none', cursor: 'pointer' }}
              onClick={() => setExpanded(open ? null : key)}
            >
              <span className="mono text-tx-3" style={{ minWidth: 34 }}>#{idx + 1}</span>
              <span className="mono text-tx-3" style={{ minWidth: 78 }}>{it.created_at ? new Date(it.created_at).toLocaleTimeString() : ''}</span>
              <span className="text-[13px] text-tx-2 flex-1 truncate">
                {it.stop_reason || '—'}
                {it.thinking_content ? ` · 💭 ${it.thinking_content.length}c` : ''}
                {it.tool_calls?.length ? ` · 🔧 ${it.tool_calls.length}` : ''}
              </span>
              <span className="text-xs text-tx-faint">{it.input_tokens ?? 0}/{it.output_tokens ?? 0} tok · ${Number(it.cost_usd || 0).toFixed(4)}</span>
              <Icon name={open ? 'chevD' : 'chevR'} size={14} />
            </button>
            {open && (
              <div className="px-3.5 pb-3 flex flex-col gap-3" style={{ background: 'var(--bg-1)' }}>
                {it.thinking_content && (
                  <Section label="💭 Thinking" color="var(--med)"><pre className="mono text-[12px] text-tx-2" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{it.thinking_content}</pre></Section>
                )}
                {it.response_content && (
                  <Section label="Response"><Markdown>{it.response_content}</Markdown></Section>
                )}
                {it.tool_calls?.length ? (
                  <Section label="Tool calls">
                    {it.tool_calls.map((tc, ti) => (
                      <div key={ti} style={{ paddingLeft: 10, borderLeft: '2px solid var(--high)', marginBottom: 6 }}>
                        <div className="mono text-[12px] text-tx" style={{ fontWeight: 600 }}>🔧 {tc.name}</div>
                        <JsonView value={tc.input} />
                      </div>
                    ))}
                  </Section>
                ) : null}
                {it.tool_results?.length ? (
                  <Section label="Tool results">
                    {it.tool_results.map((tr, ri) => (
                      <div key={ri} style={{ paddingLeft: 10, borderLeft: `2px solid ${tr.is_error ? 'var(--crit)' : 'var(--ok)'}`, marginBottom: 6 }}>
                        <div className="mono text-[12px] text-tx-3">{tr.is_error ? '❌' : '✅'} {tr.tool_use_id}</div>
                        <JsonView value={tr.content} />
                      </div>
                    ))}
                  </Section>
                ) : null}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function Section({ label, color, children }: { label: string; color?: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-xs font-semibold mb-1" style={{ color: color || 'var(--tx-2)' }}>{label}</div>
      {children}
    </div>
  )
}

/* ---------------- Chain of custody ---------------- */
interface CocLog { timestamp?: string; event_type?: string; details?: Record<string, unknown>; tokens_used?: number }
interface CocLlm { created_at?: string; model?: string; input_tokens?: number; output_tokens?: number; cost_usd?: number; duration_ms?: number; stop_reason?: string; has_thinking?: boolean; thinking_content?: string; tool_calls?: unknown[] }
interface Coc {
  investigation?: { created_at?: string; workflow_id?: string; trigger_type?: string; priority?: string }
  logs?: CocLog[]
  llm_interactions?: CocLlm[]
  otel_trace_id?: string
}

const TYPE_COLORS: Record<string, string> = {
  created: 'var(--med)',
  agent_started: 'var(--ok)',
  iteration_start: 'var(--high)',
  iteration_complete: 'var(--ok)',
  error: 'var(--crit)',
  budget_blocked: 'var(--crit)',
  failed: 'var(--crit)',
  approval_requested: 'var(--accent)',
  approval_granted: 'var(--ok)',
  status_change: 'var(--med)',
  agent_finished: 'var(--ok)',
  llm_call: 'var(--med)',
}

function CustodyTab({ coc }: { coc: Coc | null }) {
  if (!coc || (!coc.logs?.length && !coc.llm_interactions?.length && !coc.investigation)) {
    return <div className="text-sm text-tx-3 py-10 text-center">No chain-of-custody data available for this investigation.</div>
  }

  const events: { ts?: string; type: string; label: string; details?: Record<string, unknown>; tokens?: number }[] = []
  if (coc.investigation) {
    events.push({
      ts: coc.investigation.created_at,
      type: 'created',
      label: 'Investigation Created',
      details: {
        workflow_id: coc.investigation.workflow_id,
        trigger_type: coc.investigation.trigger_type,
        priority: coc.investigation.priority,
      },
    })
  }
  (coc.logs || []).forEach((l) => {
    events.push({ ts: l.timestamp, type: l.event_type || 'event', label: (l.event_type || 'event').replace(/_/g, ' '), details: l.details, tokens: l.tokens_used })
  })
  ;(coc.llm_interactions || []).forEach((it) => {
    events.push({
      ts: it.created_at,
      type: 'llm_call',
      label: `LLM Call · ${it.model}`,
      details: {
        input_tokens: it.input_tokens,
        output_tokens: it.output_tokens,
        cost_usd: it.cost_usd,
        duration_ms: it.duration_ms,
        stop_reason: it.stop_reason,
        has_thinking: it.has_thinking || !!it.thinking_content,
        tool_count: (it.tool_calls || []).length,
      },
    })
  })
  events.sort((a, b) => new Date(a.ts || 0).getTime() - new Date(b.ts || 0).getTime())

  return (
    <div className="card card-sq">
      {coc.otel_trace_id && (
        <div className="card-h"><span className="tag">OTEL {coc.otel_trace_id.slice(0, 8)}…</span><span className="grow" /><span className="text-xs text-tx-3">{events.length} events</span></div>
      )}
      <div className="card-b" style={{ maxHeight: 520, overflow: 'auto' }}>
        <div className="flex flex-col">
          {events.map((ev, idx) => (
            <div key={idx} className="flex items-start gap-3 py-2" style={{ borderBottom: idx < events.length - 1 ? '1px solid var(--line-soft)' : 'none' }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: TYPE_COLORS[ev.type] || 'var(--tx-faint)', marginTop: 6, flexShrink: 0 }} />
              <span className="mono text-tx-3" style={{ minWidth: 78 }}>{ev.ts ? new Date(ev.ts).toLocaleTimeString() : ''}</span>
              <div className="flex-1 min-w-0">
                <span className="text-[13px] font-medium text-tx-2 capitalize">{ev.label}</span>
                {ev.tokens ? <span className="text-xs text-tx-3 ml-2">{ev.tokens} tok</span> : null}
                {ev.details && Object.keys(ev.details).length > 0 && (
                  <div style={{ marginTop: 4 }}><JsonView value={ev.details} /></div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

/* ---------------- File body (extension-aware) ----------------
   .md → rendered Markdown · .json → pretty + syntax-highlighted ·
   everything else → raw monospace text. */
function FileBody({ name, content }: { name: string; content: string }) {
  const lower = name.toLowerCase()
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return <Markdown>{content}</Markdown>
  if (lower.endsWith('.json')) return <JsonView value={content} />
  return (
    <pre className="mono text-[12px] text-tx-2" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}>
      {content}
    </pre>
  )
}

/* ---------------- JSON viewer (pretty + syntax-highlighted) ----------------
   Accepts a value or a raw string. A string that isn't valid JSON falls back
   to plain monospace text (so tool results that are prose still render). */
const JSON_PRE_STYLE = { whiteSpace: 'pre-wrap' as const, wordBreak: 'break-word' as const, margin: 0 }

function JsonView({ value }: { value: unknown }) {
  let pretty: string
  if (typeof value === 'string') {
    try {
      pretty = JSON.stringify(JSON.parse(value), null, 2)
    } catch {
      return <pre className="mono text-[12px] text-tx-3" style={JSON_PRE_STYLE}>{value}</pre>
    }
  } else {
    try {
      pretty = JSON.stringify(value, null, 2)
    } catch {
      return <pre className="mono text-[12px] text-tx-3" style={JSON_PRE_STYLE}>{String(value)}</pre>
    }
  }
  return (
    <pre
      className="mono text-[12px]"
      style={{ ...JSON_PRE_STYLE, color: 'var(--tx-2)' }}
      dangerouslySetInnerHTML={{ __html: highlightJson(pretty) }}
    />
  )
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** colourise a pretty-printed JSON string. Input is HTML-escaped first, so the
 *  result is safe to inject (keys/strings/numbers/bools/null get token colours). */
function highlightJson(pretty: string): string {
  return escapeHtml(pretty).replace(
    /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (m) => {
      let color = 'var(--med)' // numbers
      if (m.charAt(0) === '"') color = /:\s*$/.test(m) ? 'var(--accent-2)' : 'var(--ok)'
      else if (m === 'true' || m === 'false') color = 'var(--high)'
      else if (m === 'null') color = 'var(--tx-faint)'
      return `<span style="color:${color}">${m}</span>`
    },
  )
}

/* ---------------- Callout ---------------- */
function Callout({ tone, title, markdown, children }: { tone: 'info' | 'warn' | 'error'; title: string; markdown?: boolean; children: ReactNode }) {
  const map = {
    info: { bg: 'var(--med-dim)', fg: 'var(--med)' },
    warn: { bg: 'var(--high-dim)', fg: 'var(--high)' },
    error: { bg: 'var(--crit-dim)', fg: 'var(--crit)' },
  }
  const c = map[tone]
  return (
    <div style={{ background: c.bg, border: `1px solid ${c.fg}33`, borderRadius: 'var(--r)', padding: '12px 14px' }}>
      <div className="text-xs font-semibold uppercase tracking-[0.05em] mb-1" style={{ color: c.fg }}>{title}</div>
      <div className="text-[13px] text-tx-2 leading-relaxed" style={markdown ? undefined : { whiteSpace: 'pre-wrap' }}>{children}</div>
    </div>
  )
}
