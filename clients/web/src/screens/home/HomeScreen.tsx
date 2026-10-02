import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ConsoleScreenProps } from '../../shared/types'
import { approvalsApi, type NeedsYouItem } from '../../services/api'
import './home.css'

const POLL_MS = 20_000
const HOLD_MS = 1600
const VISIBLE = 4

function headline(count: number): string {
  if (count === 0) return 'Board clear.'
  if (count === 1) return '1 decision waits on you. Everything else is running.'
  return `${count} decisions wait on you. Everything else is running.`
}

/** Naive timestamps from the API are UTC. `Date.parse` would read them as local. */
export function parseCreatedAt(createdAt: string): number {
  const hasZone = /(?:Z|[+-]\d{2}:\d{2})$/.test(createdAt)
  return Date.parse(hasZone ? createdAt : `${createdAt}Z`)
}

function waited(createdAt: string): string {
  const then = parseCreatedAt(createdAt)
  if (Number.isNaN(then)) return ''
  const minutes = Math.max(0, Math.floor((Date.now() - then) / 60_000))
  if (minutes < 1) return 'Waited under a minute'
  if (minutes < 60) return `Waited ${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rem = minutes % 60
  return rem ? `Waited ${hours}h ${rem}m` : `Waited ${hours}h`
}

function errorText(error: unknown, fallback: string): string {
  const data = (error as { response?: { data?: { detail?: unknown } } })?.response?.data
  if (typeof data?.detail === 'string' && data.detail.trim()) return data.detail
  const message = (error as { message?: string })?.message
  return message && message.trim() ? message : fallback
}

function HoldApprove({ disabled, onConfirm }: { disabled: boolean; onConfirm: () => void }) {
  const [holding, setHolding] = useState(false)
  const timer = useRef<number | null>(null)
  const disabledRef = useRef(disabled)
  disabledRef.current = disabled

  const clear = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current)
      timer.current = null
    }
    setHolding(false)
  }

  const start = () => {
    if (disabledRef.current || timer.current !== null) return
    setHolding(true)
    timer.current = window.setTimeout(() => {
      timer.current = null
      setHolding(false)
      if (disabledRef.current) return
      onConfirm()
    }, HOLD_MS)
  }

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
    },
    [],
  )

  return (
    <button
      type="button"
      className={holding ? 'btn danger home-hold holding' : 'btn danger home-hold'}
      disabled={disabled}
      aria-label="Approve. Press and hold to confirm; this cannot be undone."
      onPointerDown={(event) => {
        if (event.button != null && event.button !== 0) return
        event.currentTarget.setPointerCapture?.(event.pointerId)
        start()
      }}
      onPointerUp={clear}
      onPointerCancel={clear}
      onKeyDown={(event) => {
        if (event.repeat || (event.key !== ' ' && event.key !== 'Enter')) return
        event.preventDefault()
        start()
      }}
      onKeyUp={(event) => {
        if (event.key === ' ' || event.key === 'Enter') clear()
      }}
    >
      <span className="fill" aria-hidden="true" />
      <span className="label">{holding ? 'Keep holding…' : 'Approve'}</span>
    </button>
  )
}

function DecisionCard({
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
  const meta = [item.kind, item.case_id, waited(item.created_at)].filter(Boolean).join(' · ')

  return (
    <article className="card">
      <div className="card-b">
        <h3>{item.title}</h3>
        <p className="home-meta">{meta}</p>
        {item.reason && <p className="home-reason">{item.reason}</p>}
        <div className="home-actions">
          {item.reversibility === 'reversible' ? (
            <button type="button" className="btn primary" disabled={busy} onClick={() => onApprove(item.source_id)}>
              Approve
            </button>
          ) : (
            <HoldApprove disabled={busy} onConfirm={() => onApprove(item.source_id)} />
          )}
          {rejecting ? (
            <form
              className="home-reject"
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
          {item.case_id && (
            <Link className="btn ghost" to={`/cases?case=${encodeURIComponent(item.case_id)}`}>
              Open case
            </Link>
          )}
        </div>
      </div>
    </article>
  )
}

export default function HomeScreen(_props: ConsoleScreenProps) {
  const [items, setItems] = useState<NeedsYouItem[]>([])
  const [count, setCount] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const busyRef = useRef<string | null>(null)
  const loadTicket = useRef(0)

  const load = useCallback(async () => {
    const ticket = ++loadTicket.current
    try {
      const res = await approvalsApi.needsYou()
      if (ticket !== loadTicket.current) return
      setItems(res.data.items)
      setCount(res.data.count)
      setError(null)
    } catch (err) {
      if (ticket !== loadTicket.current) return
      setError(errorText(err, 'Could not load what needs you'))
    }
  }, [])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => {
      void load()
    }, POLL_MS)
    return () => window.clearInterval(id)
  }, [load])

  const run = async (id: string, act: () => Promise<unknown>) => {
    if (busyRef.current) return
    busyRef.current = id
    setBusy(id)
    try {
      await act()
      await load()
    } catch (err) {
      setError(errorText(err, 'Could not update that decision'))
    } finally {
      busyRef.current = null
      setBusy(null)
    }
  }

  const visible = showAll ? items : items.slice(0, VISIBLE)
  const hidden = items.length - visible.length

  return (
    <div className="home-screen">
      {count !== null && <p className="home-headline">{headline(count)}</p>}
      {error && (
        <p className="section" role="alert">
          {error}
        </p>
      )}
      {/* The setup list lands later; this slot stays empty until then. */}
      <section className="home-setup" aria-label="Setup" />
      <section className="section" aria-label="Needs your attention">
        <div className="home-head">
          <h2>Needs your attention</h2>
          <Link to="/cases">Cases →</Link>
        </div>
        <div className="home-cards">
          {visible.map((item) => (
            <DecisionCard
              key={item.source_id}
              item={item}
              busy={busy !== null}
              onApprove={(id) => void run(id, () => approvalsApi.approve(id))}
              onReject={(id, reason) => void run(id, () => approvalsApi.reject(id, reason))}
            />
          ))}
        </div>
        {hidden > 0 && (
          <button type="button" className="btn ghost" style={{ marginTop: 12 }} onClick={() => setShowAll(true)}>
            {hidden} more waiting
          </button>
        )}
      </section>
    </div>
  )
}
