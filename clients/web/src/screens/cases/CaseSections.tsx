import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { utcDay, utcDayClock } from '../../shared/utc'
import { casesApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import { Icon } from '../../shared/icons'
import { EmptyState } from '../../shared/ui'
import { errorDetail } from './caseFormat'

type Phase = 'loading' | 'ready' | 'error'

function fmtDT(s?: string): string {
  return utcDayClock(s) ?? '—'
}
function fmtD(s?: string): string {
  return utcDay(s) ?? '—'
}
function initials(name?: string): string {
  if (!name) return '—'
  const parts = name.trim().split(/[\s._@-]+/).filter(Boolean)
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || name[0].toUpperCase()
}

export function useResource<T>(caseId: string, run: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<string | null>(null)
  const [key, setKey] = useState(0)
  const reload = useCallback(() => setKey((k) => k + 1), [])
  // run is recreated each render; we intentionally re-fetch only on id/key
  const runRef = useRef(run)
  runRef.current = run
  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setError(null)
    runRef
      .current()
      .then((d) => {
        if (!cancelled) {
          setData(d)
          setPhase('ready')
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(errorDetail(e, 'Failed to load'))
          setPhase('error')
        }
      })
    return () => {
      cancelled = true
    }
  }, [caseId, key])
  return { data, phase, error, reload }
}

export type Resource<T> = ReturnType<typeof useResource<T>>

export function SectionCard({
  title,
  count,
  action,
  wide,
  bare,
  children,
}: {
  title: string
  count?: ReactNode
  action?: ReactNode
  wide?: boolean
  /** Side-panel look: a plain titled block, no card chrome. */
  bare?: boolean
  children: ReactNode
}) {
  if (bare) {
    return (
      <section className="side-sec">
        <div className="side-sec-head">
          <h3>{title}</h3>
          {count != null && <span>{count}</span>}
          <span className="grow" />
          {action}
        </div>
        {children}
      </section>
    )
  }
  return (
    <div
      className={`bg-panel border border-line rounded-lg shadow-panel overflow-hidden${wide ? ' span-2' : ''}`}
    >
      <div className="flex items-center gap-2.5 px-[18px] py-[15px] border-b border-line-soft">
        <h3 className="text-[14.5px]">{title}</h3>
        {count != null && <span className="text-xs text-tx-3">{count}</span>}
        <span className="flex-1" />
        {action}
      </div>
      {children}
    </div>
  )
}

function MiniEmpty({ title, body, icon = 'info' }: { title: string; body?: string; icon?: Parameters<typeof EmptyState>[0]['icon'] }) {
  return <EmptyState compact icon={icon} title={title} body={body} />
}

function MiniLoading({ title, icon = 'info', table = false }: { title: string; icon?: Parameters<typeof EmptyState>[0]['icon']; table?: boolean }) {
  return <EmptyState loading compact table={table} icon={icon} title={title} />
}

function AddBtn({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button className="btn ghost icon" title={on ? 'Cancel' : 'Add'} onClick={onClick}>
      <Icon name={on ? 'close' : 'plus'} size={14} />
    </button>
  )
}

export const inputCls =
  'bg-bg-2 border border-line rounded-md px-2.5 py-[7px] text-[13px] text-tx outline-hidden focus:border-accent-line w-full'

export function EvidenceCard({ caseId, title = 'Evidence' }: { caseId: string; title?: string }) {
  const { data, phase, error, reload } = useResource<Schema<'CaseEvidenceSchema'>[]>(caseId, () =>
    casesApi.getEvidence(caseId).then((r) => r.data.evidence),
  )
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ name: '', evidence_type: 'file', description: '', file_path: '' })
  const [writeError, setWriteError] = useState<string | null>(null)
  const items = data || []

  const submit = async () => {
    if (!form.name.trim()) return
    setWriteError(null)
    try {
      await casesApi.addEvidence(caseId, {
        name: form.name,
        description: form.description || null,
        evidence_type: form.evidence_type,
        file_path: form.file_path || null,
      })
    } catch (e) {
      setWriteError(errorDetail(e, 'Couldn’t add the evidence'))
      return
    }
    setForm({ name: '', evidence_type: 'file', description: '', file_path: '' })
    setAdding(false)
    reload()
  }

  return (
    <SectionCard
      bare
      title={title}
      count={phase === 'ready' ? `${items.length} item${items.length === 1 ? '' : 's'}` : undefined}
      action={<AddBtn on={adding} onClick={() => setAdding((v) => !v)} />}
    >
      {adding && (
        <div className="py-3 grid grid-cols-1 gap-2.5">
          <input className={inputCls} placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <select className={inputCls} value={form.evidence_type} onChange={(e) => setForm({ ...form, evidence_type: e.target.value })}>
            {['file', 'screenshot', 'log', 'url', 'other'].map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <input className={inputCls} placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <input className={inputCls} placeholder={form.evidence_type === 'url' ? 'URL' : 'File path'} value={form.file_path} onChange={(e) => setForm({ ...form, file_path: e.target.value })} />
          <button className="btn primary justify-self-start" onClick={submit}>Add evidence</button>
        </div>
      )}
      {writeError && <p role="alert" className="text-crit">{writeError}</p>}
      {phase === 'loading' && <MiniLoading icon="doc" title="Loading evidence…" />}
      {phase === 'error' && <p className="muted">Couldn’t load evidence: {error}</p>}
      {phase === 'ready' && items.length === 0 && <MiniEmpty icon="doc" title="No evidence attached" body="Add screenshots, URLs, logs, or files that support this case." />}
      {items.length > 0 && (
        <ul className="side-rows">
          {items.map((e) => {
            const hash = e.file_hash_sha256 || e.file_hash_md5
            const meta = [e.evidence_type, fmtDT(e.collected_at ?? undefined), e.collected_by || 'Unknown', hash && `${hash.slice(0, 12)}…`].filter(Boolean).join(' · ')
            return (
              <li key={e.evidence_id ?? e.name} title={e.description || undefined}>
                <div className="side-row-main">{e.name}</div>
                <div className="side-meta">{meta}</div>
              </li>
            )
          })}
        </ul>
      )}
    </SectionCard>
  )
}

const PRIO_ORDER = { critical: 0, high: 1, medium: 2, low: 3, unknown: 4 } as const
type TaskPriority = keyof typeof PRIO_ORDER
const isTaskPriority = (v: string | undefined): v is TaskPriority =>
  v !== undefined && Object.prototype.hasOwnProperty.call(PRIO_ORDER, v)
/** Sort rank for a task's priority; the unknown band sorts last. Any other name sorts as medium. */
const prioOrder = (v: string | undefined) => PRIO_ORDER[isTaskPriority(v) ? v : 'medium']
type CaseTask = Schema<'CaseTaskSchema'>
export function useTasks(caseId: string) {
  return useResource<CaseTask[]>(caseId, () => casesApi.getTasks(caseId).then((r) => r.data.tasks))
}
export function TasksView({ caseId, resource: { data, phase, error, reload }, bare }: { caseId: string; resource: Resource<CaseTask[]>; bare?: boolean }) {
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ title: '', priority: 'medium', assignee: '', due_date: '' })
  const [writeError, setWriteError] = useState<string | null>(null)
  const tasks = (data || []).slice().sort((a, b) => {
    const ad = a.status === 'completed' ? 1 : 0
    const bd = b.status === 'completed' ? 1 : 0
    if (ad !== bd) return ad - bd
    return prioOrder(a.priority ?? undefined) - prioOrder(b.priority ?? undefined)
  })
  const done = tasks.filter((t) => t.status === 'completed').length
  const pct = tasks.length ? Math.round((done / tasks.length) * 100) : 0

  const toggle = async (t: CaseTask) => {
    if (t.task_id == null) return
    const next = t.status === 'completed' ? 'pending' : 'completed'
    setWriteError(null)
    try {
      await casesApi.updateTask(caseId, t.task_id, {
        status: next,
        ...(next === 'completed' ? { completed_at: new Date().toISOString() } : {}),
      })
    } catch (e) {
      setWriteError(errorDetail(e, 'Couldn’t update the task'))
      return
    }
    reload()
  }
  const submit = async () => {
    if (!form.title.trim()) return
    setWriteError(null)
    try {
      await casesApi.addTask(caseId, {
        title: form.title,
        assignee: form.assignee || null,
        due_date: form.due_date || null,
        priority: form.priority,
      })
    } catch (e) {
      setWriteError(errorDetail(e, 'Couldn’t add the task'))
      return
    }
    setForm({ title: '', priority: 'medium', assignee: '', due_date: '' })
    setAdding(false)
    reload()
  }

  return (
    <SectionCard
      bare={bare}
      title="Tasks"
      count={phase === 'ready' ? `${done}/${tasks.length} done` : undefined}
      action={<AddBtn on={adding} onClick={() => setAdding((v) => !v)} />}
    >
      <div className={bare ? 'pt-2' : 'px-[18px] pt-3'}>
        <div className="h-1.5 bg-bg-3 rounded-full overflow-hidden">
          <div className="h-full bg-accent transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>
      {adding && (
        <div className={`${bare ? 'py-2' : 'px-[18px] py-3'} grid grid-cols-2 gap-2.5`}>
          <input className={`${inputCls} col-span-2`} placeholder="Task title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <select className={inputCls} value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}>
            {['critical', 'high', 'medium', 'low'].map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <input className={inputCls} placeholder="Assignee" value={form.assignee} onChange={(e) => setForm({ ...form, assignee: e.target.value })} />
          <input className={inputCls} type="date" value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} />
          <button className="btn primary" onClick={submit}>Add task</button>
        </div>
      )}
      <div className={`${bare ? 'pt-2' : 'p-[18px] pt-3'} flex flex-col gap-2.5`}>
        {writeError && <p role="alert" className="text-crit">{writeError}</p>}
        {phase === 'loading' && <MiniLoading icon="note" title="Loading tasks…" />}
        {phase === 'error' && <p className="muted">Couldn’t load tasks: {error}</p>}
        {phase === 'ready' && tasks.length === 0 && <MiniEmpty icon="note" title="No tasks yet" body="Add tasks to assign containment, validation, or follow-up work." />}
        {tasks.map((t) => {
          const overdue = t.due_date && t.status !== 'completed' && new Date(t.due_date).getTime() < Date.now()
          return (
            <div key={t.task_id ?? t.title} className={`flex items-start gap-2.5${t.status === 'completed' ? ' opacity-60' : ''}`}>
              <button className="mt-[1px] text-tx-3 hover:text-accent" onClick={() => toggle(t)} title="Toggle">
                <Icon name={t.status === 'completed' ? 'lock' : 'note'} size={15} />
              </button>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className={`text-[13px] ${t.status === 'completed' ? 'line-through text-tx-3' : 'text-tx'}`}>{t.title}</span>
                  {t.priority && <span className={`prio ${t.priority}`}>{t.priority[0].toUpperCase()}</span>}
                </div>
                {t.description && <div className="text-xs text-tx-3 mt-[2px]">{t.description}</div>}
                <div className="text-xs text-tx-faint mt-[2px] flex gap-3">
                  {t.assignee && <span>@{t.assignee}</span>}
                  {t.due_date && <span className={overdue ? 'text-crit' : ''}>Due {fmtD(t.due_date)}</span>}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </SectionCard>
  )
}

export interface Comment {
  id: string
  author: string
  content: string
  timestamp?: string
  parent_comment_id?: string
  replies?: Comment[]
}
/* Threading is a view model: the wire type uses integer comment_id /
   parent_comment_id and created_at. Normalize to string ids + `timestamp`
   so buildTree and the "most recent" sort keep working. */
function normalizeComment(c: Schema<'CaseCommentSchema'>): Comment | null {
  if (c.comment_id == null) return null
  return {
    id: String(c.comment_id),
    author: c.author || 'unknown',
    content: c.content || '',
    timestamp: c.created_at ?? undefined,
    parent_comment_id: c.parent_comment_id != null ? String(c.parent_comment_id) : undefined,
  }
}
function flatten(list: Comment[]): Comment[] {
  const out: Comment[] = []
  const walk = (c: Comment) => {
    out.push(c)
    ;(c.replies || []).forEach(walk)
  }
  list.forEach(walk)
  return out
}
function buildTree(list: Comment[]): Comment[] {
  const flat = flatten(list)
  const byId = new Map(flat.map((c) => [c.id, { ...c, replies: [] as Comment[] }]))
  const roots: Comment[] = []
  for (const c of byId.values()) {
    const parent = c.parent_comment_id ? byId.get(c.parent_comment_id) : undefined
    if (parent) parent.replies!.push(c)
    else roots.push(c)
  }
  return roots
}
function CommentNode({ c, onReply }: { c: Comment; onReply: (id: string) => void }) {
  return (
    <div>
      <div className="flex gap-2.5">
        <span className="avatar">{initials(c.author)}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2">
            <span className="text-[13px] font-medium text-tx">{c.author}</span>
            <span className="text-xs text-tx-faint whitespace-nowrap">{fmtDT(c.timestamp)}</span>
            {c.parent_comment_id && <span className="tag">reply</span>}
          </div>
          <div className="text-[13px] text-tx-2 whitespace-pre-wrap mt-[2px]">{c.content}</div>
          {!c.parent_comment_id && (
            <button className="text-xs bg-transparent text-accent-2 mt-1 hover:underline" onClick={() => onReply(c.id)}>Reply</button>
          )}
        </div>
      </div>
      {(c.replies || []).length > 0 && (
        <div className="ml-[34px] mt-3 flex flex-col gap-3 border-l border-line-soft pl-3">
          {c.replies!.map((r) => <CommentNode key={r.id} c={r} onReply={onReply} />)}
        </div>
      )}
    </div>
  )
}
export function useComments(caseId: string) {
  return useResource<Comment[]>(caseId, () =>
    casesApi.getComments(caseId).then((r) =>
      r.data.comments.map(normalizeComment).filter((c): c is Comment => c !== null),
    ),
  )
}
/** Comments shown for a case, replies included. */
export const commentCount = (data: Comment[] | null) => flatten(data || []).length
export function CommentsView({ caseId, resource: { data, phase, error, reload }, bare }: { caseId: string; resource: Resource<Comment[]>; bare?: boolean }) {
  const [text, setText] = useState('')
  const [replyTo, setReplyTo] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [writeError, setWriteError] = useState<string | null>(null)
  // newest thread first; collapsed view shows only the most recent one
  const tree = buildTree(data || []).sort(
    (a, b) => new Date(b.timestamp || 0).getTime() - new Date(a.timestamp || 0).getTime(),
  )
  const total = flatten(data || []).length
  const visible = showAll ? tree : tree.slice(0, 1)

  const submit = async () => {
    if (!text.trim()) return
    setWriteError(null)
    try {
      await casesApi.addComment(caseId, { content: text, ...(replyTo ? { parent_comment_id: Number(replyTo) } : {}) })
    } catch (e) {
      setWriteError(errorDetail(e, 'Couldn’t post the comment'))
      return
    }
    setText('')
    setReplyTo(null)
    reload()
  }

  return (
    <SectionCard bare={bare} title="Comments" count={phase === 'ready' ? `${total}` : undefined} wide>
      <div className={`${bare ? 'pt-2' : 'p-[18px]'} flex flex-col gap-4`}>
        {phase === 'loading' && <MiniLoading icon="note" title="Loading comments…" />}
        {phase === 'error' && <p className="muted">Couldn’t load comments: {error}</p>}
        {phase === 'ready' && total === 0 && <MiniEmpty icon="note" title="No comments yet" body="Use comments to capture analyst notes, handoffs, and review decisions." />}
        {visible.map((c) => <CommentNode key={c.id} c={c} onReply={setReplyTo} />)}
        {tree.length > 1 && (
          <button
            className="text-xs bg-transparent text-accent-2 hover:underline self-start"
            onClick={() => setShowAll((v) => !v)}
          >
            {showAll ? 'Show less' : `Show all ${tree.length} comments`}
          </button>
        )}
        <div className="border-t border-line-soft pt-3">
          {replyTo && (
            <div className="text-xs text-tx-3 mb-1.5 flex items-center gap-2">
              Replying to a comment
              <button className="text-accent-2 hover:underline" onClick={() => setReplyTo(null)}>cancel</button>
            </div>
          )}
          <div className={`flex gap-2.5 ${bare ? 'flex-col items-stretch' : 'items-end'}`}>
            <textarea
              className={`${inputCls} resize-none`}
              rows={2}
              placeholder={replyTo ? 'Write a reply…' : 'Write a comment…'}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button className="btn primary" onClick={submit}><Icon name="send" size={13} /> {replyTo ? 'Reply' : 'Post'}</button>
          </div>
          {writeError && <p role="alert" className="text-crit mt-1.5">{writeError}</p>}
        </div>
      </div>
    </SectionCard>
  )
}

type CaseIOC = Schema<'CaseIOCSchema'>
function threatLevel(i: CaseIOC) {
  if (i.is_false_positive) return { label: 'False positive', cls: 'status closed' }
  const t = (i.threat_level || '').toLowerCase()
  if (t === 'high' || t === 'critical') return { label: 'High Risk', cls: 'sev critical' }
  if (t === 'medium') return { label: 'Medium Risk', cls: 'sev medium' }
  if (t === 'low') return { label: 'Low Risk', cls: 'sev low' }
  return { label: 'Unknown', cls: 'status open' }
}
export function IOCsCard({ caseId }: { caseId: string }) {
  const { data, phase, error, reload } = useResource<CaseIOC[]>(caseId, () =>
    casesApi.getIOCs(caseId).then((r) => r.data.iocs),
  )
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ ioc_type: 'ip', value: '', context: '', source: '' })
  const [writeError, setWriteError] = useState<string | null>(null)
  const iocs = data || []

  const submit = async () => {
    if (!form.value.trim()) return
    setWriteError(null)
    try {
      await casesApi.addIOC(caseId, {
        ioc_type: form.ioc_type,
        value: form.value,
        context: form.context || null,
        source: form.source || null,
      })
    } catch (e) {
      setWriteError(errorDetail(e, 'Couldn’t add the IOC'))
      return
    }
    setForm({ ioc_type: 'ip', value: '', context: '', source: '' })
    setAdding(false)
    reload()
  }

  return (
    <SectionCard
      bare
      title="Indicators of Compromise"
      count={phase === 'ready' ? `${iocs.length}` : undefined}
      action={<AddBtn on={adding} onClick={() => setAdding((v) => !v)} />}
    >
      {adding && (
        <div className="py-3 grid grid-cols-1 gap-2.5">
          <select className={inputCls} value={form.ioc_type} onChange={(e) => setForm({ ...form, ioc_type: e.target.value })}>
            {['ip', 'domain', 'hash', 'url', 'email', 'other'].map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <input className={inputCls} placeholder="Value" value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} />
          <input className={inputCls} placeholder="Context" value={form.context} onChange={(e) => setForm({ ...form, context: e.target.value })} />
          <input className={inputCls} placeholder="Source" value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} />
          <button className="btn primary justify-self-start" onClick={submit}>Add IOC</button>
        </div>
      )}
      {writeError && <p role="alert" className="text-crit">{writeError}</p>}
      {phase === 'loading' && <MiniLoading icon="alert" title="Loading IOCs…" />}
      {phase === 'error' && <p className="muted">Couldn’t load IOCs: {error}</p>}
      {phase === 'ready' && iocs.length === 0 && <MiniEmpty icon="alert" title="No IOCs recorded" body="Add IPs, domains, hashes, URLs, or emails observed during investigation." />}
      {iocs.length > 0 && (
        <ul className="side-rows">
          {iocs.map((i) => {
            const value = i.value || ''
            const meta = [(i.ioc_type || '').toUpperCase(), threatLevel(i).label, i.source || 'Manual', fmtDT(i.first_seen ?? undefined)].filter(Boolean).join(' · ')
            return (
              <li key={i.ioc_id ?? value} title={i.context || undefined}>
                <div className="side-row-main mono" title={value}>{value}</div>
                <div className="side-meta">{meta}</div>
              </li>
            )
          })}
        </ul>
      )}
    </SectionCard>
  )
}
