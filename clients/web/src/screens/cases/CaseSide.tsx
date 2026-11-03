import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { casesApi, notesApi, type Note } from '../../services/api'
import { slaLevel } from '../../shared/LevelBadge'
import { StatePill } from '../../shared/StatePill'
import { useNoSideways } from '../../shared/useNoSideways'
import { utcDay } from '../../shared/utc'
import { commentCount, CommentsView, inputCls, SectionCard, TasksView, useComments, useResource, useTasks, type Resource } from './CaseSections'
import { errorDetail, timeLeft, when } from './caseFormat'
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
        <dd>{latest ? workflowNames[latest.workflow_id] || latest.workflow_name || latest.workflow_id : '—'}</dd>
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

// The server's EPISODIC_PROSE_CAP (core/memory/recall_contract.py).
const NOTE_CAP = 2000

type KnownCard = { key: string; text: string; meta: string; noteId?: string }

function noteMeta(note: { writtenBy: string; concludedAt: string }, mine: boolean): string {
  return `Memory · this tenant · ${mine ? 'you, just now' : `written by ${note.writtenBy || '—'}, ${utcDay(note.concludedAt) ?? '—'}`}`
}

function asNote(n: Note) {
  return { id: n.investigation_id, entity: n.entity_key, statement: n.statement, writtenBy: n.written_by, concludedAt: n.concluded_at }
}

/** Notes standing now on these keys; never fetched for none, which the API reads as every note. */
function useStandingNotes(keys: string[]) {
  const [notes, setNotes] = useState<ReturnType<typeof asNote>[]>([])
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const joined = keys.join('\n')
  useEffect(() => {
    setError(null)
    if (!joined) return setNotes([])
    let cancelled = false
    notesApi
      .list(joined.split('\n'))
      .then((res) => !cancelled && setNotes(res.data.notes.map(asNote)))
      .catch((e) => !cancelled && setError(errorDetail(e, 'Failed to load')))
    return () => {
      cancelled = true
    }
  }, [joined, tick])
  // Patch now, then read again: a list read before the write must not land over it.
  const patch = (next: (rows: ReturnType<typeof asNote>[]) => ReturnType<typeof asNote>[]) => {
    setNotes(next)
    setTick((t) => t + 1)
  }
  return { notes, patch, error }
}

/** Pick one of the case's entities, write one sentence, Save. Stands in for the board's Tell-mode composer. */
function NoteForm({ keys, onSaved, onCancel }: { keys: string[]; onSaved: (note: Note) => void; onCancel: () => void }) {
  const [picked, setEntity] = useState(keys[0])
  const entity = keys.includes(picked) ? picked : keys[0] // the keys can change under an open form
  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (!text.trim()) return
    setSaving(true)
    setError(null)
    try {
      onSaved((await notesApi.create({ entity_key: entity, statement: text.trim() })).data)
    } catch (err) {
      setError(errorDetail(err, 'Couldn’t save the note'))
      setSaving(false)
    }
  }
  return (
    <form className="side-note-form" onSubmit={save}>
      <select className={inputCls} aria-label="Entity" value={entity} onChange={(e) => setEntity(e.target.value)}>
        {keys.map((key) => <option key={key} value={key} title={key}>{entityShown(key)}</option>)}
      </select>
      <textarea
        className={`${inputCls} resize-none`}
        rows={3}
        maxLength={NOTE_CAP}
        aria-label="What the agents should know"
        placeholder="One sentence the agents should know about it"
        value={text}
        onChange={(e) => setText(e.target.value)}
        autoFocus
      />
      <div className="side-note-actions">
        <button type="submit" className="btn primary" disabled={saving || !text.trim()}>{saving ? 'Saving…' : 'Save'}</button>
        <button type="button" className="btn" onClick={onCancel} disabled={saving}>Cancel</button>
      </div>
      {error && <p role="alert" className="text-crit">{error}</p>}
    </form>
  )
}

function Known({ fold, hasRun, keys, onWritten }: { fold: RunFold | null; hasRun: boolean; keys: string[]; onWritten?: () => void }) {
  const canWrite = useAuth().hasPermission('findings.write')
  const standing = useStandingNotes(keys)
  const [mine, setMine] = useState<Set<string>>(() => new Set())
  const [writing, setWriting] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [writeError, setWriteError] = useState<string | null>(null)
  const recall = fold?.recall
  const read = recall && !recall.unavailable ? recall : null
  const standingIds = new Set(standing.notes.map((n) => n.id))
  const recalledIds = new Set(read?.notes.map((n) => n.id))
  // What the run read, then what stands now that it did not; a recalled note withdrawn since stays, without Withdraw.
  const cards: KnownCard[] = [
    ...(read?.notes ?? []).map((n) => ({ key: `n${n.id}`, text: n.statement, meta: noteMeta(n, false), noteId: standingIds.has(n.id) ? n.id : undefined })),
    ...standing.notes.filter((n) => !recalledIds.has(n.id)).map((n) => ({ key: `n${n.id}`, text: n.statement, meta: noteMeta(n, mine.has(n.id)), noteId: n.id })),
    ...(read?.verdicts ?? []).map((v, i) => ({ key: `v${i}`, text: [v.outcome, v.statement].filter(Boolean).join(' — '), meta: 'Verdict' })),
    ...(read?.sightings ?? []).map((s, i) => ({
      key: `s${i}`,
      text: [entityShown(s.entity), s.source, s.hits == null ? '' : `${s.hits} ${s.hits === 1 ? 'hit' : 'hits'}`].filter(Boolean).join(' · '),
      meta: 'Sighting',
    })),
    ...(read?.gaps ?? []).map((g, i) => ({ key: `g${i}`, text: [g.disposition.replace(/_/g, ' '), g.statement].filter(Boolean).join(' — '), meta: 'Gap' })),
  ]
  const saved = (note: Note) => {
    standing.patch((rows) => [asNote(note), ...rows])
    setMine((ids) => new Set(ids).add(note.investigation_id))
    setWriting(false)
    onWritten?.()
  }
  const withdraw = async (id: string) => {
    setBusy(id)
    setWriteError(null)
    try {
      await notesApi.withdraw(id)
    } catch (e) {
      // 404: someone withdrew it already, which is what was asked
      if ((e as { response?: { status?: number } })?.response?.status !== 404) {
        setWriteError(errorDetail(e, 'Couldn’t withdraw the note'))
        setBusy(null)
        return
      }
    }
    standing.patch((rows) => rows.filter((n) => n.id !== id))
    setBusy(null)
    onWritten?.()
  }
  return (
    <Block title="Known about these entities">
      {recall?.unavailable && <Prose text={`Recall did not happen: ${recall.unavailable}`} />}
      {cards.length === 0 && !recall && <p className="muted">{hasRun ? 'The run did not journal a recall.' : 'Nothing has looked at these entities yet.'}</p>}
      {cards.length === 0 && read && <Prose text={`Nothing recorded about ${read.keys.map(entityShown).join(', ') || 'these entities'}.`} />}
      {cards.length > 0 && (
        <div className="side-cards">
          {cards.map((card) => (
            <div key={card.key} className="side-card">
              <div className="side-clamp" title={card.text}>{card.text}</div>
              <div className="side-meta">{card.meta}</div>
              {canWrite && card.noteId && (
                <button type="button" className="side-more side-withdraw" disabled={busy !== null} onClick={() => withdraw(card.noteId!)}>
                  {busy === card.noteId ? 'Withdrawing…' : 'Withdraw'}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {standing.error && <p className="muted">Couldn’t load notes: {standing.error}</p>}
      {writeError && <p role="alert" className="text-crit">{writeError}</p>}
      {canWrite && keys.length > 0 && (writing
        ? <NoteForm keys={keys} onSaved={saved} onCancel={() => setWriting(false)} />
        : <button type="button" className="side-tell" onClick={() => setWriting(true)}>+ Tell the agents something</button>)}
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
function People({ caseId, owner, onWritten }: { caseId: string; owner: string; onWritten?: () => void }) {
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
          <CommentsView caseId={caseId} resource={comments} onWritten={onWritten} />
          <TasksView caseId={caseId} resource={tasks} onWritten={onWritten} />
          <TicketsView resource={tickets} />
        </div>
      )}
    </section>
  )
}

/** The Details, Alerts, Related cases, Known and People blocks of the case side panel. */
export function CaseSide({ caseId, linked, owner, latest, workflowNames, sla, closed, fold, onWritten }: {
  caseId: string
  linked: CaseLinkedFinding[]
  owner: string
  latest: CaseInvestigationRef | null
  workflowNames: Record<string, string>
  sla: Sla
  closed: boolean
  fold: RunFold | null
  /** A side-panel write succeeded; it wrote a trail row. */
  onWritten?: () => void
}) {
  const anchor = useRef<HTMLElement>(null)
  useNoSideways(anchor, 'The case side panel', '.case-side')
  // The typed keys runs recall on: the run's, then each linked alert's. Bare values never reach the notes API.
  const recalled = fold?.recall && !fold.recall.unavailable ? fold.recall.keys : []
  const keys = [...new Set([...recalled, ...linked.flatMap((f) => f.entity_keys)])]
  return (
    <>
      <Details anchor={anchor} linked={linked} latest={latest} workflowNames={workflowNames} sla={sla} closed={closed} fold={fold} />
      <Alerts items={linked} />
      <RelatedCases caseId={caseId} />
      <Known fold={fold} hasRun={latest !== null} keys={keys} onWritten={onWritten} />
      <People caseId={caseId} owner={owner} onWritten={onWritten} />
    </>
  )
}
