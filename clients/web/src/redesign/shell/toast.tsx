/* ============================================================
   Global toast / snackbar — the redesign's shared result-feedback
   surface (REDESIGN_GAPS.md §10). Scoped under .soc-console so the
   dark theme + accent apply; the viewport is rendered inside the
   shell tree (not a portal) for the same reason. Auto-dismisses
   (errors linger longer); each toast is keyboard-dismissable and
   announced via aria-live.
   ============================================================ */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { Icon } from '../shared/icons'

export type ToastKind = 'ok' | 'err' | 'info'

interface Toast {
  id: number
  kind: ToastKind
  text: string
}

interface ToastCtx {
  notify: (kind: ToastKind, text: string) => void
}

const Ctx = createContext<ToastCtx>({ notify: () => {} })

/** call from any component inside the shell to surface a transient toast */
export function useToast(): ToastCtx {
  return useContext(Ctx)
}

const TTL: Record<ToastKind, number> = { ok: 4000, info: 4000, err: 7000 }

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const idRef = useRef(0)
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map())

  const dismiss = useCallback((id: number) => {
    setToasts((t) => t.filter((x) => x.id !== id))
    const tm = timers.current.get(id)
    if (tm) {
      clearTimeout(tm)
      timers.current.delete(id)
    }
  }, [])

  const notify = useCallback(
    (kind: ToastKind, text: string) => {
      const id = (idRef.current += 1)
      setToasts((t) => [...t, { id, kind, text }])
      timers.current.set(
        id,
        setTimeout(() => dismiss(id), TTL[kind]),
      )
    },
    [dismiss],
  )

  // clear any pending timers on unmount
  useEffect(() => {
    const map = timers.current
    return () => {
      map.forEach(clearTimeout)
      map.clear()
    }
  }, [])

  return (
    <Ctx.Provider value={{ notify }}>
      {children}
      <div className="toast-viewport" aria-live="polite" aria-atomic="false">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`} role={t.kind === 'err' ? 'alert' : 'status'}>
            <span className="toast-ico">
              <Icon name={t.kind === 'err' ? 'alert' : t.kind === 'ok' ? 'check2' : 'info'} size={15} />
            </span>
            <span className="toast-text">{t.text}</span>
            <button className="toast-x" aria-label="Dismiss notification" onClick={() => dismiss(t.id)}>
              <Icon name="close" size={13} />
            </button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  )
}
