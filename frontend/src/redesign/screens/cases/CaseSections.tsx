/* ============================================================
   Case detail sections — the old CaseDetailDialog's five tabs
   (Overview / Investigation / Resolution / Collaboration /
   Details) re-skinned into the redesign's dark/Tailwind tokens
   and rendered as one scrolling page of cards. Each section
   fetches its own slice via the shared casesApi/graphApi and
   exposes the same write actions as the old MUI components.
   See REDESIGN_GAPS.md §9.
   ============================================================ */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { format } from 'date-fns'
import { casesApi } from '../../../services/api'
import { Icon } from '../../shared/icons'
import type { CaseRow } from '../../data/data'

/** the old CaseComments default — the redesign has no auth context */
const ME = 'SOC Analyst'

type Phase = 'loading' | 'ready' | 'error'

/* ---------------- shared helpers ---------------- */
function fmtDT(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? '—' : format(d, 'MMM d, yyyy · HH:mm')
}
function fmtD(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? '—' : format(d, 'MMM d, yyyy')
}
function initials(name?: string): string {
  if (!name) return '—'
  const parts = name.trim().split(/[\s._@-]+/).filter(Boolean)
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || name[0].toUpperCase()
}

/** generic GET-and-reload hook for a single case sub-resource */
function useResource<T>(caseId: string, run: () => Promise<T>) {
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
          setError((e as { message?: string })?.message || 'Failed to load')
          setPhase('error')
        }
      })
    return () => {
      cancelled = true
    }
  }, [caseId, key])
  return { data, phase, error, reload }
}

/* ---------------- presentational primitives ---------------- */
export function SectionCard({
  title,
  count,
  action,
  wide,
  children,
}: {
  title: string
  count?: ReactNode
  action?: ReactNode
  wide?: boolean
  children: ReactNode
}) {
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

function Note({ children }: { children: ReactNode }) {
  return <div className="muted text-center py-6 text-[13px] px-[18px]">{children}</div>
}

function AddBtn({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button className="btn ghost icon" title={on ? 'Cancel' : 'Add'} onClick={onClick}>
      <Icon name={on ? 'close' : 'plus'} size={14} />
    </button>
  )
}

export const inputCls =
  'bg-bg-2 border border-line rounded-md px-2.5 py-[7px] text-[13px] text-tx outline-none focus:border-accent-line w-full'


/* ===================================================================
   INVESTIGATION — Evidence
   =================================================================== */
interface EvidenceItem {
  id: string
  evidence_type: string
  name: string
  description?: string
  collected_at?: string
  collected_by?: string
  hash?: string
}
export function EvidenceCard({ caseId }: { caseId: string }) {
  const { data, phase, reload } = useResource<EvidenceItem[]>(caseId, () =>
    casesApi.getEvidence(caseId).then((r) => (r.data?.evidence || []) as EvidenceItem[]),
  )
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ name: '', evidence_type: 'file', description: '', url: '' })
  const items = data || []

  const submit = async () => {
    if (!form.name.trim()) return
    await casesApi.addEvidence(caseId, {
      name: form.name,
      description: form.description,
      evidence_type: form.evidence_type,
      ...(form.evidence_type === 'url' ? { url: form.url } : { file_path: form.url }),
    })
    setForm({ name: '', evidence_type: 'file', description: '', url: '' })
    setAdding(false)
    reload()
  }

  return (
    <SectionCard
      title="Evidence"
      count={`${items.length} item${items.length === 1 ? '' : 's'}`}
      wide
      action={<AddBtn on={adding} onClick={() => setAdding((v) => !v)} />}
    >
      {adding && (
        <div className="px-[18px] py-3 border-b border-line-soft grid grid-cols-[1fr_140px] gap-2.5">
          <input className={inputCls} placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <select className={inputCls} value={form.evidence_type} onChange={(e) => setForm({ ...form, evidence_type: e.target.value })}>
            {['file', 'screenshot', 'log', 'url', 'other'].map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <input className={`${inputCls} col-span-2`} placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <input className={inputCls} placeholder={form.evidence_type === 'url' ? 'URL' : 'File path'} value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
          <button className="btn primary" onClick={submit}>Add evidence</button>
        </div>
      )}
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th>Type</th><th>Name</th><th>Description</th><th>Collected</th><th>By</th><th>Hash</th></tr></thead>
          <tbody>
            {phase === 'loading' && <tr><td colSpan={6}><Note>Loading…</Note></td></tr>}
            {phase === 'ready' && items.length === 0 && <tr><td colSpan={6}><Note>No data here.</Note></td></tr>}
            {items.map((e) => (
              <tr key={e.id}>
                <td><span className="tag">{e.evidence_type}</span></td>
                <td>{e.name}</td>
                <td className="muted">{e.description || '—'}</td>
                <td className="muted">{fmtDT(e.collected_at)}</td>
                <td className="muted">{e.collected_by || 'Unknown'}</td>
                <td className="mono muted">{e.hash ? `${e.hash.slice(0, 12)}…` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </SectionCard>
  )
}

/* ===================================================================
   RESOLUTION — Resolution steps (from case payload, read-only)
   =================================================================== */
export interface ResolutionStep {
  description?: string
  action_taken?: string
  result?: string
}
export function ResolutionStepsCard({ steps }: { steps: ResolutionStep[] }) {
  return (
    <SectionCard title="Resolution steps" count={`${steps.length}`}>
      <div className="p-[18px] flex flex-col gap-3">
        {steps.length === 0 && <Note>No data here.</Note>}
        {steps.map((s, i) => (
          <div key={i} className="flex gap-2.5">
            <span className="text-ok mt-[1px]">✓</span>
            <div className="min-w-0">
              <div className="text-[13px] text-tx">{s.description || '—'}</div>
              <div className="text-xs text-tx-3 mt-[2px]">
                Action: {s.action_taken || '—'} · Result: {s.result || 'Pending'}
              </div>
            </div>
          </div>
        ))}
      </div>
    </SectionCard>
  )
}

/* ===================================================================
   RESOLUTION — Tasks
   =================================================================== */
interface Task {
  id: string
  title: string
  description?: string
  priority?: string
  assignee?: string
  due_date?: string
  status: string
  completed_at?: string
}
const PRIO_ORDER: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 }
export function TasksCard({ caseId }: { caseId: string }) {
  const { data, phase, reload } = useResource<Task[]>(caseId, () =>
    casesApi.getTasks(caseId).then((r) => (r.data?.tasks || []) as Task[]),
  )
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ title: '', priority: 'medium', assignee: '', due_date: '' })
  const tasks = (data || []).slice().sort((a, b) => {
    const ad = a.status === 'completed' ? 1 : 0
    const bd = b.status === 'completed' ? 1 : 0
    if (ad !== bd) return ad - bd
    return (PRIO_ORDER[a.priority || 'medium'] ?? 2) - (PRIO_ORDER[b.priority || 'medium'] ?? 2)
  })
  const done = tasks.filter((t) => t.status === 'completed').length
  const pct = tasks.length ? Math.round((done / tasks.length) * 100) : 0

  const toggle = async (t: Task) => {
    const next = t.status === 'completed' ? 'pending' : 'completed'
    await casesApi.updateTask(caseId, t.id, {
      status: next,
      ...(next === 'completed' ? { completed_at: new Date().toISOString() } : {}),
    })
    reload()
  }
  const submit = async () => {
    if (!form.title.trim()) return
    await casesApi.addTask(caseId, {
      title: form.title,
      assignee: form.assignee || undefined,
      due_date: form.due_date || undefined,
      priority: form.priority,
    })
    setForm({ title: '', priority: 'medium', assignee: '', due_date: '' })
    setAdding(false)
    reload()
  }

  return (
    <SectionCard
      title="Tasks"
      count={`${done}/${tasks.length} done`}
      action={<AddBtn on={adding} onClick={() => setAdding((v) => !v)} />}
    >
      <div className="px-[18px] pt-3">
        <div className="h-1.5 bg-bg-3 rounded-full overflow-hidden">
          <div className="h-full bg-accent transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>
      {adding && (
        <div className="px-[18px] py-3 grid grid-cols-2 gap-2.5">
          <input className={`${inputCls} col-span-2`} placeholder="Task title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <select className={inputCls} value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}>
            {['critical', 'high', 'medium', 'low'].map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <input className={inputCls} placeholder="Assignee" value={form.assignee} onChange={(e) => setForm({ ...form, assignee: e.target.value })} />
          <input className={inputCls} type="date" value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} />
          <button className="btn primary" onClick={submit}>Add task</button>
        </div>
      )}
      <div className="p-[18px] pt-3 flex flex-col gap-2.5">
        {phase === 'loading' && <Note>Loading…</Note>}
        {phase === 'ready' && tasks.length === 0 && <Note>No data here.</Note>}
        {tasks.map((t) => {
          const overdue = t.due_date && t.status !== 'completed' && new Date(t.due_date).getTime() < Date.now()
          return (
            <div key={t.id} className={`flex items-start gap-2.5${t.status === 'completed' ? ' opacity-60' : ''}`}>
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

/* ===================================================================
   RESOLUTION — SLA
   =================================================================== */
interface SLA {
  id: string
  policy_name: string
  status: 'active' | 'breached' | 'met' | 'paused'
  due_date: string
  created_at: string
  breached_at?: string
  paused_at?: string
  paused_duration_seconds?: number
}
const SLA_STATUS_CLASS: Record<string, string> = {
  active: 'status open',
  breached: 'sla danger',
  met: 'status closed',
  paused: 'sla warn',
}
export function SLACard({ caseId }: { caseId: string }) {
  const { data, phase, reload } = useResource<SLA | null>(caseId, () =>
    casesApi.getSLA(caseId).then((r) => (r.data?.sla ?? null) as SLA | null),
  )
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (data?.status !== 'active') return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [data?.status])

  const act = async (fn: () => Promise<unknown>) => {
    await fn()
    reload()
  }

  let remainingTxt = '—'
  let pct = 0
  let barColor = 'bg-accent'
  if (data) {
    if (data.status === 'breached') { remainingTxt = 'SLA breached'; pct = 100; barColor = 'bg-crit' }
    else if (data.status === 'met') { remainingTxt = 'SLA met'; pct = 100; barColor = 'bg-ok' }
    else if (data.status === 'paused') { remainingTxt = 'SLA paused'; barColor = 'bg-high' }
    else {
      const due = new Date(data.due_date).getTime()
      const start = new Date(data.created_at).getTime()
      const total = Math.max(1, due - start)
      const remaining = due - now
      pct = Math.min(100, Math.max(0, ((total - remaining) / total) * 100))
      if (pct > 80) barColor = 'bg-crit'
      else if (pct > 60) barColor = 'bg-high'
      if (remaining <= 0) remainingTxt = 'Overdue'
      else {
        const s = Math.floor(remaining / 1000)
        const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60
        remainingTxt = [d && `${d}d`, h && `${h}h`, m && `${m}m`, `${sec}s`].filter(Boolean).join(' ')
      }
    }
  }

  return (
    <SectionCard title="SLA">
      <div className="p-[18px]">
        {phase === 'loading' && <Note>Loading…</Note>}
        {phase === 'ready' && !data && <Note>No data here.</Note>}
        {data && (
          <>
            <div className="flex items-center gap-2 mb-3">
              <span className="tag">{data.policy_name}</span>
              <span className={SLA_STATUS_CLASS[data.status] || 'status open'}>{data.status}</span>
            </div>
            <div className="text-[15px] font-mono mb-2">{remainingTxt}</div>
            <div className="h-1.5 bg-bg-3 rounded-full overflow-hidden mb-3">
              <div className={`h-full ${barColor}`} style={{ width: `${pct}%` }} />
            </div>
            <div className="kv">
              <div className="row"><span className="k">Due</span><span className="val">{fmtDT(data.due_date)}</span></div>
              <div className="row"><span className="k">Created</span><span className="val">{fmtDT(data.created_at)}</span></div>
              {data.breached_at && <div className="row"><span className="k">Breached</span><span className="val text-crit">{fmtDT(data.breached_at)}</span></div>}
            </div>
            <div className="flex gap-2 mt-3">
              {data.status === 'active' && <button className="btn ghost" onClick={() => act(() => casesApi.pauseSLA(caseId))}><Icon name="pause" size={13} /> Pause</button>}
              {data.status === 'paused' && <button className="btn ghost" onClick={() => act(() => casesApi.resumeSLA(caseId))}><Icon name="play" size={13} /> Resume</button>}
            </div>
          </>
        )}
      </div>
    </SectionCard>
  )
}

/* ===================================================================
   COLLABORATION — Comments (threaded)
   =================================================================== */
interface Comment {
  id: string
  author: string
  content: string
  timestamp?: string
  parent_comment_id?: string
  replies?: Comment[]
}
/* the API shape: integer comment_id / parent_comment_id, created_at timestamp.
   Normalize to the redesign Comment (string ids, `timestamp`) so threading
   (buildTree matches id ↔ parent_comment_id) and the "most recent" sort work. */
interface RawComment {
  comment_id: number
  author: string
  content: string
  created_at?: string
  parent_comment_id?: number | null
}
function normalizeComment(c: RawComment): Comment {
  return {
    id: String(c.comment_id),
    author: c.author,
    content: c.content,
    timestamp: c.created_at,
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
          <div className="flex items-center gap-2">
            <span className="text-[13px] font-medium text-tx">{c.author}</span>
            <span className="text-xs text-tx-faint">{fmtDT(c.timestamp)}</span>
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
export function CommentsCard({ caseId }: { caseId: string }) {
  const { data, phase, reload } = useResource<Comment[]>(caseId, () =>
    casesApi.getComments(caseId).then((r) => ((r.data?.comments || []) as RawComment[]).map(normalizeComment)),
  )
  const [text, setText] = useState('')
  const [replyTo, setReplyTo] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  // newest thread first; collapsed view shows only the most recent one
  const tree = buildTree(data || []).sort(
    (a, b) => new Date(b.timestamp || 0).getTime() - new Date(a.timestamp || 0).getTime(),
  )
  const total = flatten(data || []).length
  const visible = showAll ? tree : tree.slice(0, 1)

  const submit = async () => {
    if (!text.trim()) return
    await casesApi.addComment(caseId, { content: text, author: ME, ...(replyTo ? { parent_comment_id: Number(replyTo) } : {}) })
    setText('')
    setReplyTo(null)
    reload()
  }

  return (
    <SectionCard title="Comments" count={`${total}`} wide>
      <div className="p-[18px] flex flex-col gap-4">
        {phase === 'loading' && <Note>Loading…</Note>}
        {phase === 'ready' && total === 0 && <Note>No data here.</Note>}
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
          <div className="flex gap-2.5 items-end">
            <textarea
              className={`${inputCls} resize-none`}
              rows={2}
              placeholder={replyTo ? 'Write a reply…' : 'Write a comment…'}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button className="btn primary" onClick={submit}><Icon name="send" size={13} /> {replyTo ? 'Reply' : 'Post'}</button>
          </div>
        </div>
      </div>
    </SectionCard>
  )
}

/* ===================================================================
   COLLABORATION — Watchers
   =================================================================== */
interface Watcher {
  user_id: string
  added_at?: string
}
export function WatchersCard({ caseId }: { caseId: string }) {
  const { data, phase, reload } = useResource<Watcher[]>(caseId, () =>
    casesApi.getWatchers(caseId).then((r) => (r.data?.watchers || []) as Watcher[]),
  )
  const [adding, setAdding] = useState(false)
  const [val, setVal] = useState('')
  const watchers = data || []

  const add = async () => {
    if (!val.trim()) return
    await casesApi.addWatcher(caseId, val.trim())
    setVal('')
    setAdding(false)
    reload()
  }
  const remove = async (uid: string) => {
    await casesApi.removeWatcher(caseId, uid)
    reload()
  }

  return (
    <SectionCard
      title="Watchers"
      count={`${watchers.length}`}
      action={<AddBtn on={adding} onClick={() => setAdding((v) => !v)} />}
    >
      {adding && (
        <div className="px-[18px] py-3 border-b border-line-soft flex gap-2.5">
          <input className={inputCls} placeholder="analyst@example.com" value={val} onChange={(e) => setVal(e.target.value)} />
          <button className="btn primary" onClick={add}>Add</button>
        </div>
      )}
      <div className="p-[18px] flex flex-col gap-2.5">
        {phase === 'loading' && <Note>Loading…</Note>}
        {phase === 'ready' && watchers.length === 0 && <Note>No data here.</Note>}
        {watchers.map((w) => (
          <div key={w.user_id} className="flex items-center gap-2.5">
            <span className="avatar">{initials(w.user_id)}</span>
            <div className="min-w-0 flex-1">
              <div className="text-[13px] text-tx">{w.user_id}</div>
              <div className="text-xs text-tx-faint">Watching since {fmtD(w.added_at)}</div>
            </div>
            <span className="watcher-active" title="Receiving notifications"><span className="wa-dot" />Active</span>
            <button className="btn ghost icon" title="Remove" onClick={() => remove(w.user_id)}><Icon name="trash" size={14} /></button>
          </div>
        ))}
      </div>
    </SectionCard>
  )
}

/* ===================================================================
   DETAILS — IOCs
   =================================================================== */
interface IOC {
  id: string
  ioc_type: string
  value: string
  description?: string
  source?: string
  first_seen?: string
  is_whitelisted?: boolean
  enrichment_data?: { threat_score?: number }
}
function threatLevel(i: IOC): { label: string; cls: string } {
  if (i.is_whitelisted) return { label: 'Whitelisted', cls: 'status closed' }
  const s = i.enrichment_data?.threat_score ?? 0
  if (s > 7) return { label: 'High Risk', cls: 'sev critical' }
  if (s > 4) return { label: 'Medium Risk', cls: 'sev medium' }
  return { label: 'Low Risk', cls: 'sev low' }
}
export function IOCsCard({ caseId }: { caseId: string }) {
  const { data, phase, reload } = useResource<IOC[]>(caseId, () =>
    casesApi.getIOCs(caseId).then((r) => (r.data?.iocs || []) as IOC[]),
  )
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ ioc_type: 'ip', value: '', description: '', source: '' })
  const iocs = data || []

  const submit = async () => {
    if (!form.value.trim()) return
    await casesApi.addIOC(caseId, { ioc_type: form.ioc_type, value: form.value, description: form.description, source: form.source })
    setForm({ ioc_type: 'ip', value: '', description: '', source: '' })
    setAdding(false)
    reload()
  }

  return (
    <SectionCard
      title="Indicators of Compromise"
      count={`${iocs.length}`}
      wide
      action={<AddBtn on={adding} onClick={() => setAdding((v) => !v)} />}
    >
      {adding && (
        <div className="px-[18px] py-3 border-b border-line-soft grid grid-cols-[120px_1fr] gap-2.5">
          <select className={inputCls} value={form.ioc_type} onChange={(e) => setForm({ ...form, ioc_type: e.target.value })}>
            {['ip', 'domain', 'hash', 'url', 'email', 'other'].map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <input className={inputCls} placeholder="Value" value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} />
          <input className={inputCls} placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <input className={inputCls} placeholder="Source" value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} />
          <button className="btn primary col-span-2 justify-self-start" onClick={submit}>Add IOC</button>
        </div>
      )}
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th>Type</th><th>Value</th><th>Description</th><th>Source</th><th>Threat</th><th>First seen</th></tr></thead>
          <tbody>
            {phase === 'loading' && <tr><td colSpan={6}><Note>Loading…</Note></td></tr>}
            {phase === 'ready' && iocs.length === 0 && <tr><td colSpan={6}><Note>No data here.</Note></td></tr>}
            {iocs.map((i) => {
              const tl = threatLevel(i)
              return (
                <tr key={i.id}>
                  <td><span className="tag">{i.ioc_type.toUpperCase()}</span></td>
                  <td className="mono" title={i.value}>{i.value.length > 40 ? `${i.value.slice(0, 40)}…` : i.value}</td>
                  <td className="muted">{i.description || '—'}</td>
                  <td className="muted">{i.source || 'Manual'}</td>
                  <td><span className={tl.cls}><span className="dot" />{tl.label}</span></td>
                  <td className="muted">{fmtDT(i.first_seen)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </SectionCard>
  )
}

/* ===================================================================
   DETAILS — Related cases
   =================================================================== */
interface LinkedCase {
  link_id: string
  related_case_id: string
  related_case_title?: string
  related_case_status?: string
  related_case_priority?: string
  relationship_type?: string
  created_at?: string
}
const REL_LABEL: Record<string, string> = {
  duplicate_of: 'Duplicate Of',
  related_to: 'Related To',
  caused_by: 'Caused By',
  follows: 'Follows',
  blocks: 'Blocks',
}
export function RelatedCasesCard({ caseId, rows, onSelect }: { caseId: string; rows: CaseRow[]; onSelect: (id: string) => void }) {
  const { data, phase, reload } = useResource<LinkedCase[]>(caseId, () =>
    casesApi.getLinkedCases(caseId).then((r) => (r.data?.linked_cases || []) as LinkedCase[]),
  )
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ relationship_type: 'related_to', related_case_id: '' })
  const linked = data || []
  const candidates = rows.filter((r) => r.id !== caseId)

  const submit = async () => {
    if (!form.related_case_id) return
    await casesApi.linkCase(caseId, form.related_case_id, form.relationship_type)
    setForm({ relationship_type: 'related_to', related_case_id: '' })
    setAdding(false)
    reload()
  }

  return (
    <SectionCard
      title="Related cases"
      count={`${linked.length}`}
      action={<AddBtn on={adding} onClick={() => setAdding((v) => !v)} />}
    >
      {adding && (
        <div className="px-[18px] py-3 border-b border-line-soft grid grid-cols-[140px_1fr] gap-2.5">
          <select className={inputCls} value={form.relationship_type} onChange={(e) => setForm({ ...form, relationship_type: e.target.value })}>
            {Object.keys(REL_LABEL).map((t) => <option key={t} value={t}>{REL_LABEL[t]}</option>)}
          </select>
          <select className={inputCls} value={form.related_case_id} onChange={(e) => setForm({ ...form, related_case_id: e.target.value })}>
            <option value="">Select case…</option>
            {candidates.map((c) => <option key={c.id} value={c.id}>{c.id} — {c.title}</option>)}
          </select>
          <button className="btn primary col-span-2 justify-self-start" onClick={submit}>Link case</button>
        </div>
      )}
      <div className="p-[18px] flex flex-col gap-2.5">
        {phase === 'loading' && <Note>Loading…</Note>}
        {phase === 'ready' && linked.length === 0 && <Note>No data here.</Note>}
        {linked.map((l) => (
          <div key={l.link_id} className="flex items-center gap-2.5 clickable" onClick={() => onSelect(l.related_case_id)}>
            <span className="tag">{REL_LABEL[l.relationship_type || ''] || l.relationship_type || '—'}</span>
            <div className="min-w-0 flex-1">
              <div className="text-[13px] text-tx truncate">{l.related_case_title || l.related_case_id}</div>
              <div className="flex items-center gap-2 mt-[2px]">
                <span className="id-cell">{l.related_case_id}</span>
                {l.related_case_status && <span className={`status ${l.related_case_status}`}>{l.related_case_status}</span>}
                {l.related_case_priority && <span className={`prio ${l.related_case_priority}`}>{l.related_case_priority}</span>}
              </div>
            </div>
            <span className="text-xs text-tx-faint">{fmtD(l.created_at)}</span>
          </div>
        ))}
      </div>
    </SectionCard>
  )
}

/* ===================================================================
   DETAILS — Audit log (read-only)
   =================================================================== */
interface AuditEntry {
  id: string
  user?: string
  action: string
  field_name?: string
  old_value?: string
  new_value?: string
  timestamp?: string
}
export function AuditLogCard({ caseId }: { caseId: string }) {
  const { data, phase } = useResource<AuditEntry[]>(caseId, () =>
    casesApi.getAuditLog(caseId).then((r) => (r.data?.audit_log || []) as AuditEntry[]),
  )
  const entries = data || []
  return (
    <SectionCard title="Audit log" count={`${entries.length}`} wide>
      <div className="p-[18px] flex flex-col gap-2.5">
        {phase === 'loading' && <Note>Loading…</Note>}
        {phase === 'ready' && entries.length === 0 && <Note>No data here.</Note>}
        {entries.map((a) => (
          <div key={a.id} className="flex gap-2.5 text-[13px]">
            <span className="avatar">{initials(a.user)}</span>
            <div className="min-w-0 flex-1">
              <span className="text-tx-2">
                <b className="text-tx">{a.user || 'system'}</b> {a.action}
                {a.field_name && <> <span className="tag">{a.field_name}</span></>}
                {a.field_name && (a.old_value || a.new_value) && (
                  <> from <span className="text-crit">{a.old_value || '—'}</span> to <span className="text-ok">{a.new_value || '—'}</span></>
                )}
              </span>
              <div className="text-xs text-tx-faint mt-[2px]">{fmtDT(a.timestamp)}</div>
            </div>
          </div>
        ))}
      </div>
    </SectionCard>
  )
}

/* ===================================================================
   OVERVIEW — Recent activity (from case payload, read-only)
   =================================================================== */
export interface Activity {
  description?: string
  activity_type?: string
  timestamp?: string
}
export function ActivityCard({ activities }: { activities: Activity[] }) {
  return (
    <SectionCard title="Recent activity" count={`${activities.length}`}>
      <div className="p-[18px] flex flex-col gap-3">
        {activities.length === 0 && <Note>No data here.</Note>}
        {activities.slice(0, 12).map((a, i) => (
          <div key={i} className="text-[13px]">
            <div className="text-tx-2">{a.description || '—'}</div>
            <div className="text-xs text-tx-faint mt-[2px]">{a.activity_type || 'event'} · {fmtDT(a.timestamp)}</div>
          </div>
        ))}
      </div>
    </SectionCard>
  )
}
