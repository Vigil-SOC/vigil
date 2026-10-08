import { useState, type ReactNode } from 'react'
import { casesApi } from '../../services/api'
import { LevelBadge, slaLevel } from '../../shared/LevelBadge'
import { MeterBar } from '../../shared/MeterBar'
import { NotMeasured } from '../../shared/NotMeasured'
import { commentCount, CommentsView, SectionCard, TasksView, useComments, useResource, useTasks, type Resource } from './CaseSections'
import { money, timeLeft, when } from './caseFormat'
import type { RunFold } from './caseFold'
import type { CaseInvestigationRef } from './useCases'

type Sla = { due: string; health: string } | null

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="side-block">
      <h3 className="side-title">{title}</h3>
      {children}
    </section>
  )
}

function Details({ latest, workflowNames, sla, closed, fold }: {
  latest: CaseInvestigationRef | null
  workflowNames: Record<string, string>
  sla: Sla
  closed: boolean
  fold: RunFold | null
}) {
  const left = sla && !closed ? timeLeft(sla.due) : '' // a closed case's clock has stopped
  const entities = fold?.recall && !fold.recall.unavailable ? fold.recall.keys.join(', ') : ''
  return (
    <Block title="Details">
      <dl className="side-kv">
        <dt>Workflow</dt>
        <dd>{latest ? workflowNames[latest.workflow_id] || latest.workflow_id : '—'}</dd>
        <dt>Limit</dt>
        <dd>{latest && latest.max_cost_usd > 0 ? money(latest.max_cost_usd) : '—'}</dd>
        <dt>Resolve by</dt>
        <dd>
          {sla ? when(sla.due) : '—'}
          {left && (
            <>
              {' · '}
              <span className={`case-sla ${slaLevel(sla?.health) ?? ''}`}>{left}</span>
            </>
          )}
        </dd>
        <dt>Entities</dt>
        <dd>{entities || '—'}</dd>
      </dl>
    </Block>
  )
}

/** X, the bar and the word all read the latest run: the server's budget_health is computed from the same two numbers. */
function Cost({ latest }: { latest: CaseInvestigationRef | null }) {
  const measured = !!latest && latest.max_cost_usd > 0
  const level = latest ? slaLevel(latest.budget_health) : null
  return (
    <Block title="Cost">
      {measured ? (
        <>
          <div className="side-cost">
            <span>{money(latest.cost_usd)} of {money(latest.max_cost_usd)}</span>
            <LevelBadge level={level} className={`side-level ${level ?? ''}`} />
          </div>
          <MeterBar pct={(latest.cost_usd / latest.max_cost_usd) * 100} level={level} label="Cost against the limit" />
        </>
      ) : (
        <NotMeasured />
      )}
    </Block>
  )
}

function Known({ fold }: { fold: RunFold | null }) {
  const recall = fold?.recall
  const cards = recall && !recall.unavailable
    ? [
        ...recall.verdicts.map((v) => ({ text: [v.outcome, v.statement].filter(Boolean).join(' — '), kind: 'Verdict' })),
        ...recall.sightings.map((s) => ({
          text: [s.entity, s.source, s.hits == null ? '' : `${s.hits} ${s.hits === 1 ? 'hit' : 'hits'}`].filter(Boolean).join(' · '),
          kind: 'Sighting',
        })),
        ...recall.gaps.map((g) => ({ text: [g.disposition.replace(/_/g, ' '), g.statement].filter(Boolean).join(' — '), kind: 'Gap' })),
      ]
    : []
  return (
    <Block title="Known about these entities">
      {!recall && <p className="muted">The run did not journal a recall.</p>}
      {recall?.unavailable && <p className="muted">Recall did not happen: {recall.unavailable}</p>}
      {recall && !recall.unavailable && cards.length === 0 && (
        <p className="muted">Nothing recorded about {recall.keys.join(', ') || 'these entities'}.</p>
      )}
      {cards.length > 0 && (
        <div className="side-cards">
          {cards.map((card, i) => (
            <div key={i} className="side-card">
              <div>{card.text}</div>
              <div className="side-meta">{card.kind}</div>
            </div>
          ))}
        </div>
      )}
    </Block>
  )
}

type Ticket = { id: string; label: string }

function useTickets(caseId: string) {
  return useResource<Ticket[]>(caseId, () =>
    casesApi.getEscalations(caseId).then((res) =>
      (res.data.escalations || []).map((row) => ({
        id: String(row.escalation_id ?? row.escalated_to),
        label: [row.escalated_to, row.reason].filter(Boolean).join(' — ') || 'Ticket',
      })),
    ),
  )
}

function TicketsView({ resource: { data, phase } }: { resource: Resource<Ticket[]> }) {
  const rows = data || []
  return (
    <SectionCard bare title="Linked tickets">
      <div className="side-list">
        {phase === 'loading' && <p className="muted">Loading tickets…</p>}
        {phase === 'error' && <p className="muted">Couldn’t load tickets.</p>}
        {phase === 'ready' && rows.length === 0 && <p className="muted">No linked tickets.</p>}
        {rows.map((row) => <p key={row.id}>{row.label}</p>)}
      </div>
    </SectionCard>
  )
}

function initialsOf(name: string): string {
  const parts = name.trim().split(/[\s._@-]+/).filter(Boolean)
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || '—'
}

/** Comments, tasks and tickets load once here (open or not) and feed the collapsed line, the summary and the cards. */
function People({ caseId, owner }: { caseId: string; owner: string }) {
  const [open, setOpen] = useState(false)
  const comments = useComments(caseId)
  const tasks = useTasks(caseId)
  const tickets = useTickets(caseId)
  // Never a made-up count: the number appears only once that list has loaded.
  const count = (r: Resource<unknown[]>) => (r.phase === 'ready' ? String(r.data?.length ?? 0) : '—')
  const n = commentCount(comments.data)
  const line =
    comments.phase === 'ready'
      ? `${owner}, ${n} ${n === 1 ? 'comment' : 'comments'}`
      : comments.phase === 'error'
        ? `${owner}, comments unavailable`
        : owner
  return (
    <section className="side-block">
      <button type="button" className="side-people-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span>People · {line}</span>
        <span className={`side-chev${open ? ' open' : ''}`} aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="side-people">
          <div className="side-owner">
            <span className="avatar">{owner === '—' ? '—' : initialsOf(owner)}</span>
            <span>{owner}</span>
          </div>
          <div className="side-meta">
            Comments {comments.phase === 'ready' ? n : '—'} · Tasks {count(tasks as Resource<unknown[]>)} · Tickets {count(tickets as Resource<unknown[]>)}
          </div>
          <CommentsView caseId={caseId} resource={comments} bare />
          <TasksView caseId={caseId} resource={tasks} bare />
          <TicketsView resource={tickets} />
        </div>
      )}
    </section>
  )
}

/** The Details, Cost, Known and People blocks of the case side panel. */
export function CaseSide({ caseId, owner, latest, workflowNames, sla, closed, fold }: {
  caseId: string
  owner: string
  latest: CaseInvestigationRef | null
  workflowNames: Record<string, string>
  sla: Sla
  closed: boolean
  fold: RunFold | null
}) {
  return (
    <>
      <Details latest={latest} workflowNames={workflowNames} sla={sla} closed={closed} fold={fold} />
      <Cost latest={latest} />
      <Known fold={fold} />
      <People caseId={caseId} owner={owner} />
    </>
  )
}
