// site/*.md is edited where it lives; this card shows the revision the daemon
// applied, any refusal, each item with its file, line and owner, the open site
// proposal for a person to download or discard, and the site questions.
import { useEffect, useState, type ReactNode } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { configApi } from '../../services/api'
import type { Schema } from '../../services/apiTypes'
import { errorText } from '../../shared/errorText'
import type { ConsoleScreenProps } from '../../shared/types'
import { Popup, SettingsCard } from '../../shared/ui'
import { utcDayClock } from '../../shared/utc'
import type { SectionProps } from './types'

type Site = Schema<'SiteContextResponse'>
type OwnerRef = Schema<'SiteOwnerRef'>
type Proposal = Schema<'SiteProposalResponse'>
type Provenance = Schema<'SiteProposalProvenance'>
type Question = Schema<'SiteQuestion'>

const SITE_AGENT = { id: 'site_context', name: 'Site context agent' }
const PROPOSAL_COLOR: Record<string, string> = { open: 'var(--ac)', applied: 'var(--good)', outdated: 'var(--fair)', discarded: 'var(--tx3)' }
const CHANGE_COLOR: Record<string, string> = { added: 'var(--good)', changed: 'var(--fair)', removed: 'var(--poor)' }
const LIST_TITLE: Record<string, string> = { zones: 'Zones', crown_jewels: 'Crown jewels', owners: 'Owners', blind_spots: 'Blind spots', telemetry: 'Telemetry' }
const QUESTION_TEXT: Record<string, string> = {
  unplaced: 'In no zone',
  contradicted: 'Stated critical, but no crown jewel covers it',
  unowned: 'No owner resolves',
}

// The proposal's stamps are UTC with no offset, as applied_at is.
const utcStamp = (at?: string | null) =>
  at ? `${utcDayClock(/[Zz]|[+-]\d\d:?\d\d$/.test(at) ? at : `${at}Z`) ?? at} UTC` : null

const STATE_COLOR: Record<string, string> = { ok: 'var(--good)', rejected: 'var(--poor)', degraded: 'var(--fair)' }

// The daemon writes applied_at as UTC with no offset.
const utcText = (at: string) => new Date(/[Zz]|[+-]\d\d:?\d\d$/.test(at) ? at : `${at}Z`).toLocaleString()

function Where({ file, line }: { file: string; line: number }) {
  return (
    <span className="mono text-xs text-tx-3">
      {file}:{line}
    </span>
  )
}

// The server resolved each owner; an unresolved one shows its reason as given.
function Owners({ owners }: { owners?: OwnerRef[] }) {
  if (!owners?.length) return <span className="text-tx-3">no owner declared</span>
  return (
    <>
      {owners.map((o, i) => (
        <span key={`${o.file}:${o.line}`}>
          {i > 0 && ', '}
          {o.resolved ? (
            <span className="text-tx">{o.resolved}</span>
          ) : (
            <span style={{ color: 'var(--fair)' }}>unassigned: {o.unassigned}</span>
          )}
        </span>
      ))}
    </>
  )
}

function Selectors({ selectors }: { selectors: string[] }) {
  return <span className="mono text-xs text-tx-2">{selectors.join(', ')}</span>
}

function Group({ title, count, empty = 'None declared.', children }: { title: string; count: number; empty?: string; children: ReactNode }) {
  return (
    <div className="mb-4">
      <h4 className="text-sm font-semibold text-tx mb-1.5">
        {title} <span className="text-tx-3 font-normal">{count}</span>
      </h4>
      {count ? (
        <ul className="list-none m-0 p-0 flex flex-col gap-2 [overflow-wrap:anywhere]">{children}</ul>
      ) : (
        <p className="text-xs text-tx-3">{empty}</p>
      )}
    </div>
  )
}

function Item({ head, at, children }: { head: ReactNode; at?: { file?: string | null; line?: number | null }; children?: ReactNode }) {
  return (
    <li className="text-sm text-tx-2 border-b border-line pb-2 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-x-2">
        {head}
        {at?.file && <Where file={at.file} line={at.line ?? 0} />}
      </div>
      <div className="flex flex-col gap-0.5 mt-0.5">{children}</div>
    </li>
  )
}

function Files({ files }: { files: { file: string; text: string }[] }) {
  if (!files.length) return null
  return (
    <div className="flex flex-col gap-1.5">
      {files.map((f) => (
        <details key={f.file}>
          <summary className="mono text-xs text-tx-2 cursor-pointer">{f.file}</summary>
          <pre className="mono text-xs text-tx mt-2 px-3 py-2.5 rounded bg-bg-2 whitespace-pre-wrap [overflow-wrap:anywhere] max-h-80 overflow-y-auto">
            {f.text}
          </pre>
        </details>
      ))}
    </div>
  )
}

// What one diffed item rests on: a cited call, or a person's answer quoted as the agent gave it.
function RestsOn({ p, openChat }: { p?: Provenance; openChat?: ConsoleScreenProps['openChat'] }) {
  if (!p) return <span className="text-xs text-tx-3">No provenance recorded.</span>
  if (p.source === 'answer') {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-xs text-tx-3">
          Answered by <span className="text-tx-2">{p.person ?? p.person_id ?? 'unknown'}</span>
          {p.at && <> · {utcStamp(p.at)}</>}
          {p.conversation_id && openChat && (
            <>
              {' · '}
              <button type="button" className="bg-transparent text-accent-2 hover:underline" onClick={() => openChat(undefined, { conversationId: p.conversation_id! })}>
                Open the conversation
              </button>
            </>
          )}
        </span>
        <blockquote
          className="m-0 pl-2.5 text-sm text-tx whitespace-pre-wrap"
          style={{ borderLeft: '2px solid var(--vio)' }}
          aria-label="Quoted answer, as the agent gave it"
        >
          <span className="text-xs text-tx-3 block">Quoted, as the agent gave it</span>“{p.answer}”
        </blockquote>
      </div>
    )
  }
  const args = p.arguments == null ? '' : typeof p.arguments === 'string' ? p.arguments : JSON.stringify(p.arguments)
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-tx-3">
        Cites <span className="mono text-tx-2">{p.tool ?? 'a call'}</span>
        {p.server && <> on <span className="mono text-tx-2">{p.server}</span></>}
        {p.ref && <> ({p.ref})</>}
        {p.at && <> · {utcStamp(p.at)}</>}
      </span>
      {args && (
        <code className="mono text-xs text-tx-2 px-2 py-1 rounded bg-bg-2 whitespace-pre-wrap [overflow-wrap:anywhere] max-h-32 overflow-y-auto">
          {args}
        </code>
      )}
    </div>
  )
}

function ProposalBlock({
  proposal,
  canChat,
  openChat,
  onDiscard,
  notify,
}: {
  proposal: Proposal
  canChat: boolean
  openChat?: ConsoleScreenProps['openChat']
  onDiscard: () => void
  notify?: SectionProps['notify']
}) {
  const [downloading, setDownloading] = useState(false)
  const discarded = proposal.discarded as { at?: string; by?: string; reason?: string } | null
  const by = proposal.saved_by.username ?? proposal.saved_by.person_id ?? proposal.saved_by.agent_id ?? 'unknown'
  const diff = proposal.diff ?? []
  const lists = Array.from(new Set(diff.map((d) => d.kind)))
  const questions = proposal.questions ?? []
  const skipped = proposal.not_evaluated ?? []

  const download = async () => {
    setDownloading(true)
    try {
      const res = await configApi.getSiteProposalArchive().catch(async (e) => {
        // a blob response carries its error body as a Blob
        const data = e?.response?.data
        if (data instanceof Blob) {
          try { e.response.data = JSON.parse(await data.text()) } catch { /* not JSON */ }
        }
        throw e
      })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `site-proposal-${proposal.id.slice(0, 8)}.tar.gz`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      notify?.('err', errorText(e, 'Could not download the site proposal.'))
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="mb-4 px-3 py-2.5 rounded border border-line [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 mb-2">
        <h4 className="text-sm font-semibold text-tx m-0">Proposal</h4>
        <span className="chip" style={{ color: PROPOSAL_COLOR[proposal.status] ?? 'var(--tx2)' }}>
          {proposal.status}
        </span>
        <span className="text-xs text-tx-3">
          Saved by <span className="text-tx-2">{by}</span> · {utcStamp(proposal.saved_at)}
          {proposal.base_revision && (
            <> · against revision <span className="mono">{proposal.base_revision}</span></>
          )}
        </span>
      </div>
      {discarded && (
        <p className="text-sm text-tx-2 mb-2">
          Discarded{discarded.by && <> by {discarded.by}</>}
          {discarded.at && <> · {utcStamp(discarded.at)}</>}: {discarded.reason}
        </p>
      )}
      {proposal.status === 'applied' && <p className="text-sm text-tx-2 mb-2">The revision applied now holds every item it proposed.</p>}
      {proposal.status === 'outdated' && (
        <p className="text-sm text-tx-2 mb-2">A newer revision was applied since it was saved; the diff is against that revision.</p>
      )}
      {!discarded && (
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <button className="btn" onClick={download} disabled={downloading}>
            {downloading ? 'Downloading…' : 'Download'}
          </button>
          {canChat && (proposal.status === 'open' || proposal.status === 'outdated') && (
            <button className="btn" onClick={onDiscard}>
              Discard…
            </button>
          )}
          <span className="text-xs text-tx-3">
            Unpack the archive over <span className="mono">site/</span> in the declared directory and commit it with a new{' '}
            <span className="mono">REVISION</span>; Vigil applies it from there.
          </span>
        </div>
      )}
      {proposal.status !== 'applied' &&
        (lists.length ? (
          lists.map((kind) => {
            const rows = diff.filter((d) => d.kind === kind)
            return (
              <Group key={kind} title={LIST_TITLE[kind] ?? kind} count={rows.length}>
                {rows.map((d) => (
                  <Item
                    key={`${d.kind}/${d.item}`}
                    head={
                      <>
                        <span className="chip" style={{ color: CHANGE_COLOR[d.change] ?? 'var(--tx2)' }}>
                          {d.change}
                        </span>
                        <span className="text-tx font-medium">{d.item}</span>
                      </>
                    }
                  >
                    <RestsOn p={proposal.provenance?.[`${d.kind}/${d.item}`]} openChat={openChat} />
                  </Item>
                ))}
              </Group>
            )
          })
        ) : (
          <p className="text-xs text-tx-3 mb-3">No item differs from the revision applied now.</p>
        ))}
      {questions.length > 0 && (
        <Group title="Questions for people" count={questions.length}>
          {questions.map((q, i) => (
            <li key={i} className="text-sm text-tx-2">
              {q}
            </li>
          ))}
        </Group>
      )}
      {skipped.length > 0 && (
        <Group title="Sources not evaluated" count={skipped.length}>
          {skipped.map((n) => (
            <li key={n.tool} className="text-sm text-tx-2">
              <span className="mono text-tx">{n.tool}</span>: {n.reason}
              {n.where && <span className="text-xs text-tx-3"> · set its class in {n.where}</span>}
            </li>
          ))}
        </Group>
      )}
      <Files files={proposal.files ?? []} />
    </div>
  )
}

function SiteQuestions({ questions }: { questions: Question[] }) {
  return (
    <Group title="Site questions" count={questions.length} empty="None now.">
      {questions.map((q, i) => (
        <Item
          key={`${q.question}:${q.entity_key ?? q.name}:${i}`}
          head={
            <>
              <span className="chip" style={{ color: 'var(--fair)' }}>
                {q.question}
              </span>
              <span className="mono text-tx">{q.entity_key ?? q.name}</span>
              {q.question === 'unowned' && <span className="text-xs text-tx-3">{q.kind === 'crown_jewel' ? 'crown jewel' : q.kind}</span>}
            </>
          }
          at={q.file ? { file: q.file, line: q.line } : undefined}
        >
          <span className="text-xs">
            {QUESTION_TEXT[q.question] ?? q.question}
            {q.investigations != null && (
              <>
                {' · '}
                {q.investigations} investigation{q.investigations === 1 ? '' : 's'}
              </>
            )}
            {q.latest && <> · latest {utcStamp(q.latest)}</>}
          </span>
        </Item>
      ))}
    </Group>
  )
}

function DiscardDialog({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (open) {
      setReason('')
      setError(null)
    }
  }, [open])
  const submit = async () => {
    if (!reason.trim()) return
    setBusy(true)
    setError(null)
    try {
      await configApi.discardSiteProposal(reason.trim())
      onDone()
    } catch (e) {
      setError(errorText(e, 'Could not discard the site proposal.'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Popup open={open} onClose={onClose} title="Discard the site proposal" width={440}>
      <label className="field">
        <span className="field-label">Reason</span>
        <textarea
          className="field-input"
          rows={3}
          value={reason}
          required
          autoFocus
          onChange={(e) => setReason(e.target.value)}
        />
        {error ? <span className="field-hint err">{error}</span> : <span className="field-hint">Kept on the audit entry.</span>}
      </label>
      <div className="flex justify-end gap-2.5 mt-5">
        <button className="btn ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button className="btn danger" onClick={submit} disabled={busy || !reason.trim()}>
          {busy ? 'Working…' : 'Discard'}
        </button>
      </div>
    </Popup>
  )
}

export default function SiteContextCard({
  reloadKey = 0,
  notify,
  openChat,
}: {
  reloadKey?: number
  notify?: SectionProps['notify']
  openChat?: ConsoleScreenProps['openChat']
}) {
  const [site, setSite] = useState<Site | null>(null)
  const [failed, setFailed] = useState(false)
  const [reread, setReread] = useState(0)
  const [discarding, setDiscarding] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshNote, setRefreshNote] = useState<{ text: string; refused: boolean } | null>(null)
  const canChat = useAuth().hasPermission('ai_chat.use')

  const refresh = async () => {
    setRefreshing(true)
    setRefreshNote(null)
    try {
      const res = await configApi.refreshSiteProposal()
      setRefreshNote({
        text: res.data.status === 'queued' ? 'Refresh queued. The site context agent saves its proposal here when it finishes.' : res.data.message,
        refused: false,
      })
    } catch (e) {
      // 409 refused and 500 not queued carry their text as `message`; shown as returned
      const message = (e as { response?: { data?: { message?: unknown } } })?.response?.data?.message
      setRefreshNote({ text: typeof message === 'string' && message ? message : errorText(e, 'Could not start the refresh.'), refused: true })
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    configApi
      .getSite()
      .then((res) => {
        if (cancelled) return
        setSite(res.data)
        setFailed(false)
      })
      .catch(() => {
        if (cancelled) return
        setSite(null)
        setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey, reread])

  const chip = (state: string) => (
    <span className="chip" style={{ color: STATE_COLOR[state] ?? 'var(--tx2)' }}>
      {state}
    </span>
  )
  const actions = site && canChat && (
    <>
      {openChat && (
        // the dock refuses a turn while the agent is off, and says so
        <button className="btn" onClick={() => openChat(undefined, { agent: SITE_AGENT })}>
          Describe this site with Vigil
        </button>
      )}
      <button className="btn" onClick={refresh} disabled={refreshing}>
        {refreshing ? 'Starting…' : 'Refresh proposal'}
      </button>
    </>
  )
  // The proposal and the site questions show whatever the applied site's state.
  const proposalAndQuestions = site && (
    <>
      {refreshNote && (
        <p className="text-sm mb-3 [overflow-wrap:anywhere]" style={{ color: refreshNote.refused ? 'var(--fair)' : 'var(--tx1)' }} role="status">
          {refreshNote.text}
        </p>
      )}
      {site.proposal && (
        <ProposalBlock
          proposal={site.proposal}
          canChat={canChat}
          openChat={openChat}
          onDiscard={() => setDiscarding(true)}
          notify={notify}
        />
      )}
      <SiteQuestions questions={site.site_questions ?? []} />
      <DiscardDialog
        open={discarding}
        onClose={() => setDiscarding(false)}
        onDone={() => {
          setDiscarding(false)
          setReread((n) => n + 1)
        }}
      />
    </>
  )
  const line = (text: string, tone = 'text-tx-2', state?: string) => (
    <SettingsCard title="Declared site" wide actions={actions || undefined}>
      <p className={`text-sm ${tone} [overflow-wrap:anywhere] mb-3`}>
        {state && <>{chip(state)} </>}
        {text}
      </p>
      {proposalAndQuestions}
    </SettingsCard>
  )

  if (failed) return line('Could not load the declared site.')
  if (!site) return line('Loading declared site…', 'text-tx-3')
  // No directory, or a directory with no site revision applied or refused yet.
  if (site.state === 'none') return line(site.reason)
  // A directory set but no revision applied or refused yet (e.g. unreadable on the first poll).
  if (!site.declared && !site.rejected) return line(site.reason, 'text-tx-2', site.state)

  const rejected = site.rejected as { revision?: string; file?: string; line?: number; reason?: string } | null
  const zones = site.zones ?? []
  const jewels = site.crown_jewels ?? []
  const owners = site.owners ?? []
  const blind = site.blind_spots ?? []
  const telemetry = site.telemetry ?? []
  const files = site.files ?? []

  return (
    <SettingsCard title="Declared site" wide actions={actions || undefined}>
      <div className="text-xs text-tx-3 mb-3 flex flex-col gap-1 [overflow-wrap:anywhere]">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {chip(site.state)}
          <span>
            {site.declared ? (
              <>
                Revision <span className="mono">{site.revision}</span>
                {site.applied_at && <> applied {utcText(site.applied_at)}</>}
              </>
            ) : (
              'No revision applied yet'
            )}{' '}
            from <span className="mono">{site.directory}</span>. Change it there, not here.
          </span>
        </div>
        {site.reason !== rejected?.reason && <div>{site.reason}</div>}
      </div>
      {rejected && (
        <div
          className="mb-3 px-3 py-2 rounded text-sm [overflow-wrap:anywhere]"
          style={{ background: 'var(--poor-bg)', border: '1px solid var(--poor-ln)' }}
        >
          <div style={{ color: 'var(--poor)' }}>
            Revision <span className="mono">{rejected.revision ?? 'unknown'}</span> refused at{' '}
            <span className="mono">
              {rejected.file}:{rejected.line}
            </span>
          </div>
          <div className="text-tx-2">{rejected.reason}</div>
          <div className="text-xs text-tx-3 mt-1">
            {site.declared
              ? `Below is the last good revision, ${site.revision}, which still applies.`
              : 'No earlier revision applied, so nothing is declared.'}
          </div>
        </div>
      )}
      {proposalAndQuestions}
      {site.declared && (
        <>
          <Group title="Zones" count={zones.length}>
            {zones.map((z) => (
              <Item key={`${z.file}:${z.line}`} head={<span className="text-tx font-medium">{z.name}</span>} at={z}>
                {z.description && <span>{z.description}</span>}
                <Selectors selectors={z.selectors} />
                <span>
                  Owner: <Owners owners={z.owners} />
                </span>
              </Item>
            ))}
          </Group>
          <Group title="Crown jewels" count={jewels.length}>
            {jewels.map((j) => (
              <Item key={`${j.file}:${j.line}`} head={<span className="text-tx font-medium">{j.name}</span>} at={j}>
                <span>
                  Mission: {j.mission}
                  {j.zone && <> · Zone: {j.zone}</>}
                </span>
                {j.why && <span>Why: {j.why}</span>}
                <Selectors selectors={j.selectors} />
                <span>
                  Owner: <Owners owners={j.owners} />
                </span>
              </Item>
            ))}
          </Group>
          <Group title="Owners" count={owners.length}>
            {owners.map((o) => (
              <Item key={`${o.file}:${o.line}`} head={<Owners owners={[o]} />} at={o}>
                {!o.resolved && <span className="text-xs text-tx-3">Declared team: {o.team}</span>}
                <Selectors selectors={o.selectors} />
              </Item>
            ))}
          </Group>
          <Group title="Blind spots" count={blind.length}>
            {blind.map((b) => (
              <Item key={`${b.file}:${b.line}`} head={<span className="text-tx">{b.what}</span>} at={b}>
                <Selectors selectors={b.selectors} />
              </Item>
            ))}
          </Group>
          <Group title="Telemetry" count={telemetry.length}>
            {telemetry.map((t) => (
              <Item key={`${t.file}:${t.line}`} head={<span className="text-tx">{t.name}</span>} at={t}>
                <span>Covers: {t.covers}</span>
              </Item>
            ))}
          </Group>
          <Files files={files} />
        </>
      )}
    </SettingsCard>
  )
}
