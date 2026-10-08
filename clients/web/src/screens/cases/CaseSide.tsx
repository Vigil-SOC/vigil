import { useState, type ReactNode } from 'react'
import { casesApi } from '../../services/api'
import { LevelBadge, slaLevel, type Level } from '../../shared/LevelBadge'
import { MeterBar } from '../../shared/MeterBar'
import { NotMeasured } from '../../shared/NotMeasured'
import { commentCount, CommentsView, SectionCard, TasksView, useComments, useResource, useTasks, type Resource } from './CaseSections'
import { money, timeLeft, when } from './caseFormat'
import { IN_FLIGHT } from '../workflows/runRead'
import type { RunFold } from './caseFold'
import type { CaseInvestigationRef, CaseLinkedFinding } from './useCases'

type Sla = { due: string; health: string } | null

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="side-block">
      <h3 className="side-title">{title}</h3>
      {children}
    </section>
  )
}

interface Spend {
  cost: number | null
  cap: number | null
  /** The server's word, when it was read off the same two numbers shown; else derived from them. */
  level: Level
}

/** What the newest run has spent against what it was granted. `fold` is that run's fold, or null when it is another run. */
function spendOf(latest: CaseInvestigationRef | null, fold: RunFold | null): Spend {
  const inFlight = !!fold && IN_FLIGHT.includes(fold.run.status)
  const foldCap = fold?.kind === 'hunt' && fold.maxCostUsd ? fold.maxCostUsd : null
  // The refs are re-read only when a run ends, so a run in flight is read off its own fold.
  const cost = (inFlight ? fold.costUsd ?? latest?.cost_usd : latest?.cost_usd || fold?.costUsd) ?? null
  const cap = foldCap ?? (latest && latest.max_cost_usd > 0 ? latest.max_cost_usd : null)
  if (cost === null || cap === null) return { cost, cap, level: null }
  // A live number or a fold's cap outruns the server's word, which a hunt's ref never had a cap for.
  const level = inFlight || foldCap !== null ? levelOf(cost / cap) : slaLevel(latest?.budget_health)
  return { cost, cap, level }
}

/** The server's 75 / 90 cuts. */
function levelOf(ratio: number): Level {
  return ratio >= 0.9 ? 'poor' : ratio >= 0.75 ? 'fair' : 'good'
}

function Details({ latest, workflowNames, sla, closed, fold, spend }: {
  latest: CaseInvestigationRef | null
  workflowNames: Record<string, string>
  sla: Sla
  closed: boolean
  fold: RunFold | null
  spend: Spend
}) {
  const left = sla && !closed ? timeLeft(sla.due) : '' // a closed case's clock has stopped
  const entities = fold?.recall && !fold.recall.unavailable ? fold.recall.keys.join(', ') : ''
  return (
    <Block title="Details">
      <dl className="side-kv">
        <dt>Workflow</dt>
        <dd>{latest ? workflowNames[latest.workflow_id] || latest.workflow_id : '—'}</dd>
        <dt>Limit</dt>
        <dd>{spend.cap ? money(spend.cap) : '—'}</dd>
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

const ALERTS_SHOWN = 3

/** The alerts combined into this case; the first few, then "+N". */
function Alerts({ items }: { items: CaseLinkedFinding[] }) {
  const [all, setAll] = useState(false)
  if (items.length === 0) return null
  const shown = all ? items : items.slice(0, ALERTS_SHOWN)
  return (
    <Block title={`Alerts (${items.length})`}>
      <ul className="case-linked">
        {shown.map((item) => {
          const text = item.title || item.description || item.finding_id
          return (
            <li key={item.finding_id}>
              <span className="side-clamp" title={text}>{text}</span>
              {item.source_link && <a href={item.source_link} target="_blank" rel="noreferrer">Open in source</a>}
            </li>
          )
        })}
      </ul>
      {items.length > ALERTS_SHOWN && (
        <button type="button" className="side-more" onClick={() => setAll((v) => !v)}>
          {all ? 'Show fewer' : `+${items.length - ALERTS_SHOWN}`}
        </button>
      )}
    </Block>
  )
}

/** X, the bar and the word all read the newest run; the bar and word appear only once a cap is known. */
function Cost({ spend: { cost, cap, level } }: { spend: Spend }) {
  return (
    <Block title="Cost">
      {cost !== null && cost > 0 ? (
        <>
          <div className="side-cost">
            <span>{cap ? `${money(cost)} of ${money(cap)}` : money(cost)}</span>
            {cap && <LevelBadge level={level} className={`side-level ${level ?? ''}`} />}
          </div>
          {cap && <MeterBar pct={(cost / cap) * 100} level={level} label="Cost against the limit" />}
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

/** The Details, Alerts, Cost, Known and People blocks of the case side panel. */
export function CaseSide({ caseId, linked, owner, latest, workflowNames, sla, closed, fold, latestFold }: {
  caseId: string
  linked: CaseLinkedFinding[]
  owner: string
  latest: CaseInvestigationRef | null
  workflowNames: Record<string, string>
  sla: Sla
  closed: boolean
  fold: RunFold | null
  /** `fold` when it is the newest run's, else null. */
  latestFold: RunFold | null
}) {
  const spend = spendOf(latest, latestFold)
  return (
    <>
      <Details latest={latest} workflowNames={workflowNames} sla={sla} closed={closed} fold={fold} spend={spend} />
      <Alerts items={linked} />
      <Cost spend={spend} />
      <Known fold={fold} />
      <People caseId={caseId} owner={owner} />
    </>
  )
}
