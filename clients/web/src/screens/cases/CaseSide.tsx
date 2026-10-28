import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { Link } from 'react-router-dom'
import { casesApi } from '../../services/api'
import { slaLevel } from '../../shared/LevelBadge'
import { StatePill } from '../../shared/StatePill'
import { useNoSideways } from '../../shared/useNoSideways'
import { commentCount, CommentsView, SectionCard, TasksView, useComments, useResource, useTasks, type Resource } from './CaseSections'
import { timeLeft, when } from './caseFormat'
import { entityShown, type RunFold } from './caseFold'
import type { CaseInvestigationRef, CaseLinkedFinding } from './useCases'

type Sla = { due: string; health: string } | null

function Block({ title, children, anchor }: { title: string; children: ReactNode; anchor?: RefObject<HTMLElement | null> }) {
  return (
    <section className="side-block" ref={anchor}>
      <h3 className="side-title">{title}</h3>
      {children}
    </section>
  )
}

function Details({ linked, latest, workflowNames, sla, closed, fold, anchor }: {
  anchor: RefObject<HTMLElement | null>
  linked: CaseLinkedFinding[]
  latest: CaseInvestigationRef | null
  workflowNames: Record<string, string>
  sla: Sla
  closed: boolean
  fold: RunFold | null
}) {
  const left = sla && !closed ? timeLeft(sla.due) : '' // a closed case's clock has stopped
  // What the run asked about, then what the linked alerts name: a hunt's case has alerts before it has recall.
  const recalled = fold?.recall && !fold.recall.unavailable ? fold.recall.keys : []
  const entities = [...new Set([...recalled, ...linked.flatMap((f) => f.entities)])]
  return (
    <Block title="Details" anchor={anchor}>
      <dl className="side-kv">
        <dt>Workflow</dt>
        <dd>{latest ? workflowNames[latest.workflow_id] || latest.workflow_id : '—'}</dd>
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
        <dd>{entities.length ? <Entities values={entities} /> : '—'}</dd>
      </dl>
    </Block>
  )
}

const ENTITIES_SHOWN = 3
/** Before it is laid out, a value past this many characters is taken not to fit; the panel then measures it. */
const ENTITY_LONG = 24
const ENTITY_TAIL = 6

/**
 * One entity on one line. A value that fits the column is shown whole; only one wider than it is
 * cut in the middle (#2459), by CSS, so the whole value stays in the DOM: selecting and copying it
 * copies all of it.
 */
function Entity({ entity }: { entity: string }) {
  const value = entityShown(entity)
  const ref = useRef<HTMLSpanElement>(null)
  const [long, setLong] = useState(value.length > ENTITY_LONG)
  useLayoutEffect(() => {
    const box = ref.current
    const head = box?.firstElementChild as HTMLElement | null
    if (!box || !head) return
    const measure = () => {
      if (box.clientWidth === 0) return // not laid out: keep the guess
      const tail = head.nextElementSibling as HTMLElement | null
      setLong(head.scrollWidth + (tail?.offsetWidth ?? 0) > box.clientWidth)
    }
    measure()
    // the column narrows and widens (a scrollbar comes and goes): measure again, so a value that fits is never cut
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(box)
    return () => observer.disconnect()
  }, [value])
  // the head gives up only the room the tail takes, so the cut is no wider than it has to be
  useLayoutEffect(() => {
    const tail = ref.current?.querySelector<HTMLElement>('.side-ent-tail')
    if (tail && tail.offsetWidth > 0) ref.current?.style.setProperty('--ent-tail', `${tail.offsetWidth + 2}px`)
  }, [long, value])
  return (
    <span ref={ref} className={`side-ent${long ? ' long' : ''}`} title={entity}>
      <span className="side-ent-head">{long ? value.slice(0, -ENTITY_TAIL) : value}</span>
      {long && <span className="side-ent-tail">{value.slice(-ENTITY_TAIL)}</span>}
    </span>
  )
}

/** The first few entities, then "+N" for the rest (DESIGN.md §7, "Rows stay even"). One line per value, however many keys name it. */
function Entities({ values }: { values: string[] }) {
  const [all, setAll] = useState(false)
  const seen = new Set<string>()
  const distinct = values.filter((value) => {
    const shown = entityShown(value)
    if (seen.has(shown)) return false
    seen.add(shown)
    return true
  })
  const shown = all ? distinct : distinct.slice(0, ENTITIES_SHOWN)
  return (
    <div className="side-ents">
      {shown.map((value) => <Entity key={value} entity={value} />)}
      {distinct.length > ENTITIES_SHOWN && (
        <button type="button" className="side-more" aria-expanded={all} onClick={() => setAll((v) => !v)}>
          {all ? 'Show fewer' : `+${distinct.length - ENTITIES_SHOWN}`}
        </button>
      )}
    </div>
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

type Related = { id: number; caseId: string; title: string; status: string; notes: string }

/** Other Cases linked to this one, of any link type; hidden while loading, on a failed read, or when there are none. */
function RelatedCases({ caseId }: { caseId: string }) {
  const { data, phase } = useResource<Related[]>(caseId, () =>
    casesApi.getRelationships(caseId).then((res) =>
      (res.data.relationships || []).map((row) => ({
        id: row.relationship_id ?? 0,
        caseId: row.related_case_id ?? '',
        title: row.related_title || '',
        status: row.related_status || '',
        notes: row.notes || '',
      })),
    ),
  )
  if (phase !== 'ready' || !data?.length) return null
  return (
    <Block title="Related cases">
      <ul className="case-linked case-related">
        {data.map((row) => (
          <li key={row.id}>
            <span className="case-related-head">
              {row.title && <span className="side-clamp" title={row.title}>{row.title}</span>}
              {row.status && <StatePill state={row.status} />}
            </span>
            <Link className="mono case-related-id" to={`/cases?case=${encodeURIComponent(row.caseId)}`}>{row.caseId}</Link>
            {row.notes && <span className="side-meta side-clamp" title={row.notes}>{row.notes}</span>}
          </li>
        ))}
      </ul>
    </Block>
  )
}

/** A line of free text: wraps, stops at two lines, the whole of it on hover (DESIGN.md §7). */
function Prose({ text }: { text: string }) {
  return <p className="muted side-clamp" title={text}>{text}</p>
}

function Known({ fold, hasRun }: { fold: RunFold | null; hasRun: boolean }) {
  const recall = fold?.recall
  const cards = recall && !recall.unavailable
    ? [
        ...recall.verdicts.map((v) => ({ text: [v.outcome, v.statement].filter(Boolean).join(' — '), kind: 'Verdict' })),
        ...recall.sightings.map((s) => ({
          text: [entityShown(s.entity), s.source, s.hits == null ? '' : `${s.hits} ${s.hits === 1 ? 'hit' : 'hits'}`].filter(Boolean).join(' · '),
          kind: 'Sighting',
        })),
        ...recall.gaps.map((g) => ({ text: [g.disposition.replace(/_/g, ' '), g.statement].filter(Boolean).join(' — '), kind: 'Gap' })),
      ]
    : []
  return (
    <Block title="Known about these entities">
      {!recall && <p className="muted">{hasRun ? 'The run did not journal a recall.' : 'Nothing has looked at these entities yet.'}</p>}
      {recall?.unavailable && <Prose text={`Recall did not happen: ${recall.unavailable}`} />}
      {recall && !recall.unavailable && cards.length === 0 && (
        <Prose text={`Nothing recorded about ${recall.keys.map(entityShown).join(', ') || 'these entities'}.`} />
      )}
      {cards.length > 0 && (
        <div className="side-cards">
          {cards.map((card, i) => (
            <div key={i} className="side-card">
              <div className="side-clamp" title={card.text}>{card.text}</div>
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

function TicketsView({ resource: { data, phase, error } }: { resource: Resource<Ticket[]> }) {
  const rows = data || []
  return (
    <SectionCard title="Linked tickets">
      <div className="side-list">
        {phase === 'loading' && <p className="muted">Loading tickets…</p>}
        {phase === 'error' && <p className="muted">Couldn’t load tickets: {error}</p>}
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
          <CommentsView caseId={caseId} resource={comments} />
          <TasksView caseId={caseId} resource={tasks} />
          <TicketsView resource={tickets} />
        </div>
      )}
    </section>
  )
}

/** The Details, Alerts, Related cases, Known and People blocks of the case side panel. */
export function CaseSide({ caseId, linked, owner, latest, workflowNames, sla, closed, fold }: {
  caseId: string
  linked: CaseLinkedFinding[]
  owner: string
  latest: CaseInvestigationRef | null
  workflowNames: Record<string, string>
  sla: Sla
  closed: boolean
  fold: RunFold | null
}) {
  const anchor = useRef<HTMLElement>(null)
  useNoSideways(anchor, 'The case side panel', '.case-side')
  return (
    <>
      <Details anchor={anchor} linked={linked} latest={latest} workflowNames={workflowNames} sla={sla} closed={closed} fold={fold} />
      <Alerts items={linked} />
      <RelatedCases caseId={caseId} />
      <Known fold={fold} hasRun={latest !== null} />
      <People caseId={caseId} owner={owner} />
    </>
  )
}
