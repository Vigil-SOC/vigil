import { useCallback, useEffect, useRef, useState } from 'react'
import api from '../services/api'
import { noteInput } from '../services/userAction'
import { Icon } from '../shared/icons'

// What the server says about this sign-in, turned into browser-clock times. It is the
// only timer: every number comes from /auth/me, and any tab may re-read it.
interface Timing {
  /** when the read that produced it was sent */
  at: number
  /** when the sign-in goes idle; null: it has no idle limit */
  idleAt: number | null
  endsAt: number
}

const IDLE_WARN_MS = 120_000
const END_WARN_MS = 300_000
const REREAD_MS = 60_000
const CHANNEL = 'vigil.sign-in'
const DEV_MODE = import.meta.env.VITE_DEV_MODE === 'true'

const left = (ms: number) => {
  const s = Math.max(0, Math.ceil(ms / 1000))
  return s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`
}

/**
 * Two notices, from one shell-level mount: a countdown for the last two minutes before the
 * sign-in goes idle, and a note five minutes before it reaches its absolute end. Tabs share
 * the server's numbers over BroadcastChannel (localStorage events where there is none), so
 * an action in any tab clears the countdown in all of them.
 */
export default function SignInWatch() {
  const [timing, setTiming] = useState<Timing | null>(null)
  const [now, setNow] = useState(Date.now())
  const [endDismissed, setEndDismissed] = useState(false)
  const current = useRef<Timing | null>(null)
  const lastRead = useRef(0)
  const channel = useRef<BroadcastChannel | null>(null)

  const adopt = useCallback((next: Timing, share: boolean) => {
    if (current.current && next.at < current.current.at) return
    current.current = next
    setTiming(next)
    if (!share) return
    if (channel.current) channel.current.postMessage(next)
    else localStorage.setItem(CHANNEL, JSON.stringify(next))
  }, [])

  // A read carries the person-action marker only while a click or key is fresh, so the
  // timed re-reads below are not activity.
  const read = useCallback(async () => {
    lastRead.current = Date.now()
    const at = lastRead.current
    try {
      const { data } = await api.get('/auth/me')
      const s = data?.sign_in
      if (!s) return
      adopt(
        {
          at,
          idleAt: s.idle_in_seconds == null ? null : at + s.idle_in_seconds * 1000,
          endsAt: at + s.ends_in_seconds * 1000,
        },
        true,
      )
    } catch {
      // a refused read is handled where the sign-in ends; anything else is retried
    }
  }, [adopt])

  useEffect(() => {
    if (DEV_MODE) return
    const receive = (t: Timing | null) => {
      if (t && typeof t.at === 'number' && typeof t.endsAt === 'number') adopt(t, false)
    }
    let close = () => {}
    if (typeof BroadcastChannel !== 'undefined') {
      const ch = new BroadcastChannel(CHANNEL)
      channel.current = ch
      ch.onmessage = (e) => receive(e.data)
      close = () => ch.close()
    } else {
      const onStorage = (e: StorageEvent) => {
        if (e.key === CHANNEL && e.newValue) receive(JSON.parse(e.newValue))
      }
      window.addEventListener('storage', onStorage)
      close = () => window.removeEventListener('storage', onStorage)
    }

    void read()
    const tick = setInterval(() => {
      const n = Date.now()
      setNow(n)
      const t = current.current
      const since = n - lastRead.current
      const idleLeft = t?.idleAt != null ? t.idleAt - n : Infinity
      // A read from before the warning window may predate another tab's action: confirm first.
      const unconfirmed = t?.idleAt != null && idleLeft <= IDLE_WARN_MS && t.at < t.idleAt - IDLE_WARN_MS
      const due = idleLeft <= 0 || (t ? t.endsAt - n <= 0 : false)
      if (since >= REREAD_MS || (unconfirmed && since >= 2000) || (due && since >= 3000)) void read()
    }, 1000)

    // Any action while the countdown shows is the person staying: send the read as theirs.
    const onInput = () => {
      const t = current.current
      if (t?.idleAt != null && t.idleAt - Date.now() <= IDLE_WARN_MS && Date.now() - lastRead.current >= 2000) {
        noteInput()
        void read()
      }
    }
    document.addEventListener('pointerdown', onInput, { capture: true, passive: true })
    document.addEventListener('keydown', onInput, { capture: true, passive: true })
    return () => {
      clearInterval(tick)
      document.removeEventListener('pointerdown', onInput, { capture: true })
      document.removeEventListener('keydown', onInput, { capture: true })
      close()
      channel.current = null
    }
  }, [adopt, read])

  if (!timing) return null
  const idleLeft = timing.idleAt != null ? timing.idleAt - now : null
  const confirmed = timing.idleAt != null && timing.at >= timing.idleAt - IDLE_WARN_MS
  const showIdle = idleLeft != null && confirmed && idleLeft > 0 && idleLeft <= IDLE_WARN_MS
  const endLeft = timing.endsAt - now
  const showEnd = !endDismissed && endLeft > 0 && endLeft <= END_WARN_MS
  if (!showIdle && !showEnd) return null

  return (
    <div className="signin-watch">
      {showIdle && (
        <div className="toast info" role="alert">
          <span className="toast-ico"><Icon name="clock" size={15} /></span>
          <span className="toast-text">
            <span className="sr-only">This sign-in is idle and ends in under 2 minutes.</span>
            <span aria-hidden="true">Idle: you will be signed out in <b className="mono">{left(idleLeft!)}</b>.</span>
          </span>
          <button
            type="button"
            className="toast-undo"
            onClick={() => {
              noteInput()
              void read()
            }}
          >
            Stay signed in
          </button>
        </div>
      )}
      {showEnd && (
        <div className="toast info" role="status">
          <span className="toast-ico"><Icon name="clock" size={15} /></span>
          <span className="toast-text">
            Your sign-in ends at <span className="mono">{new Date(timing.endsAt).toISOString().slice(11, 16)} UTC</span>; save your work.
          </span>
          <button type="button" className="toast-x" aria-label="Dismiss" onClick={() => setEndDismissed(true)}>
            <Icon name="close" size={13} />
          </button>
        </div>
      )}
    </div>
  )
}
